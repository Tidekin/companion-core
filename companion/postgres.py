"""Native PostgreSQL sessions, transaction locks and non-authoritative wake hints.

The database owns a bounded pool. A session owns one connection until its finite
operation has finished; materialized rows can outlive it. SQL uses Psycopg's native
parameters. This module neither translates SQLite SQL nor performs external work.
"""

import hashlib
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import SecretStr

WAKE_CHANNEL = "companion_wakeup"
BUDGET_LEDGER = 1
ERASURE_LEDGER = 2


def migrate(conninfo: SecretStr, revision="head"):
    """Apply native Alembic migrations without putting credentials in configuration logs."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    if not isinstance(conninfo, SecretStr):
        raise TypeError("database connection information must be SecretStr")
    location = Path(__file__).resolve().parents[1] / "migrations" / "postgresql"
    if not location.is_dir():
        location = Path(__file__).resolve().parent / "migrations" / "postgresql"
    configuration = Config()
    configuration.set_main_option("script_location", str(location))
    engine = create_engine(
        "postgresql+psycopg://", poolclass=NullPool, hide_parameters=True,
        creator=lambda: psycopg.connect(conninfo.get_secret_value(), connect_timeout=5),
    )
    try:
        with engine.begin() as connection:
            # Serialize schema/catalog changes only. The two-int lock namespace is
            # separate from the runtime journal's bigint transaction locks.
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(1129271877,1296648019)")
            configuration.attributes["connection"] = connection
            command.upgrade(configuration, revision)
    finally:
        engine.dispose()


def _configure(connection):
    if connection.info.server_version < 160000:
        raise RuntimeError("POSTGRESQL_16_OR_NEWER_REQUIRED")
    connection.execute("SET TIME ZONE 'UTC'")


class Database:
    """Own a bounded native pool; credentials stay outside representations."""

    def __init__(self, conninfo: SecretStr, *, max_connections=4, timeout=5):
        if not isinstance(conninfo, SecretStr):
            raise TypeError("database connection information must be SecretStr")
        self._conninfo = conninfo
        self.cache_identity = hashlib.sha256(conninfo.get_secret_value().encode()).hexdigest()
        self._pool = ConnectionPool(
            conninfo.get_secret_value(), min_size=0, max_size=max_connections,
            max_waiting=16, timeout=timeout, open=False, configure=_configure,
            kwargs={"autocommit": True, "row_factory": dict_row,
                    "connect_timeout": timeout, "application_name": "companion-core"},
        )

    def __enter__(self):
        self._pool.open()
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._pool.close()

    @contextmanager
    def session(self):
        with self._pool.connection() as connection:
            session = Session(connection)
            try:
                yield session
            finally:
                session._closed = True
                # A raw, unfinished BEGIN must never be committed by pool return.
                if connection.info.transaction_status != TransactionStatus.IDLE:
                    connection.rollback()

    @contextmanager
    def listener(self):
        # LISTEN owns a separate connection rather than occupying a request slot.
        with psycopg.connect(self._conninfo.get_secret_value(), autocommit=True,
                             connect_timeout=5, application_name="companion-wakeup") as connection:
            _configure(connection)
            connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(WAKE_CHANNEL)))
            yield connection


class Session:
    """One finite owner of a connection; no connection escapes the public interface."""

    def __init__(self, connection):
        self._connection = connection
        self._closed = False
        self._ledger_locks = None

    @property
    def backend_pid(self):
        if self._closed:
            raise RuntimeError("POSTGRES_SESSION_CLOSED")
        return self._connection.info.backend_pid

    @property
    def in_transaction(self):
        return not self._closed and (
            self._connection.info.transaction_status != TransactionStatus.IDLE)

    def execute(self, statement, parameters=()):
        if self._closed:
            raise RuntimeError("POSTGRES_SESSION_CLOSED")
        with self._connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.rowcount

    def one(self, statement, parameters=()):
        if self._closed:
            raise RuntimeError("POSTGRES_SESSION_CLOSED")
        with self._connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchone()

    def rows(self, statement, parameters=()):
        if self._closed:
            raise RuntimeError("POSTGRES_SESSION_CLOSED")
        with self._connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchall()

    def bounded_read(self, statement, parameters, *, limit, timeout_ms):
        """Materialize a finite optional SELECT without aborting its caller's snapshot.

        Psycopg creates a savepoint inside an existing transaction. Read-only mode
        refuses data-changing CTEs; cancellation rolls back only this optional read.
        """
        if self._closed:
            raise RuntimeError("POSTGRES_SESSION_CLOSED")
        if type(limit) is not int or limit < 1 or type(timeout_ms) is not int or timeout_ms < 1:
            raise ValueError("invalid bounded read limits")
        active = self.in_transaction
        with self._connection.transaction():
            if not active:
                self.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            settings = self.one(
                "SELECT current_setting('transaction_read_only') AS readonly, "
                "current_setting('statement_timeout') AS timeout"
            )
            if settings["readonly"] != "on":
                raise RuntimeError("BOUNDED_READ_REQUIRES_READ_ONLY_SNAPSHOT")
            self.execute("SELECT set_config('statement_timeout',%s,true)", (f"{timeout_ms}ms",))
            with self._connection.cursor() as cursor:
                cursor.execute(sql.SQL("SELECT * FROM ({}) AS bounded_result LIMIT %s").format(
                    statement if isinstance(statement, sql.Composable) else sql.SQL(statement)),
                    (*parameters, limit))
                result = cursor.fetchmany(limit)
            self.execute("SELECT set_config('statement_timeout',%s,true)", (settings["timeout"],))
            return result

    @contextmanager
    def transaction(self, *, readonly=False, rollback=False):
        if self._closed:
            raise RuntimeError("POSTGRES_SESSION_CLOSED")
        if self._connection.info.transaction_status != TransactionStatus.IDLE:
            raise RuntimeError("POSTGRES_TRANSACTION_ALREADY_ACTIVE")
        with self._connection.transaction(force_rollback=rollback):
            self._ledger_locks = {}
            try:
                if readonly:
                    self.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                yield self
            finally:
                self._ledger_locks = None

    def custody_lock(self, key, *, shared=False):
        if self._connection.info.transaction_status != TransactionStatus.INTRANS:
            raise RuntimeError("POSTGRES_TRANSACTION_REQUIRED")
        if type(key) is not int or not -(2**63) <= key < 2**63:
            raise ValueError("invalid PostgreSQL transaction lock key")
        self.execute("SELECT pg_advisory_xact_lock_shared(%s)" if shared else
                     "SELECT pg_advisory_xact_lock(%s)", (key,))

    def notify(self):
        if self._connection.info.transaction_status != TransactionStatus.INTRANS:
            raise RuntimeError("POSTGRES_TRANSACTION_REQUIRED")
        self.execute("SELECT pg_notify(%s, %s)", (WAKE_CHANNEL, ""))

    def ledger_lock(self, domain, *, shared=False):
        """Native transaction lock, scoped to this schema and a fixed ledger domain."""
        if self._connection.info.transaction_status != TransactionStatus.INTRANS:
            raise RuntimeError("POSTGRES_TRANSACTION_REQUIRED")
        if type(domain) is not int or not 1 <= domain <= 16:
            raise ValueError("invalid ledger lock domain")
        # A managed session keeps its configured schema for this finite scope.
        # PostgreSQL retains these locks until commit/rollback; an exclusive
        # acquisition also covers later shared requests. Upgrades still acquire.
        held = self._ledger_locks.get(domain) if self._ledger_locks is not None else None
        if held is False or held is True and shared:
            return
        function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
        self.execute(sql.SQL("SELECT {}(hashtext(current_schema()),%s)").format(
            sql.Identifier(function)), (domain,))
        if self._ledger_locks is not None:
            self._ledger_locks[domain] = bool(shared)
