"""Native PostgreSQL sessions, transaction locks and non-authoritative wake hints.

The database owns a bounded pool. A session owns one connection until its finite
operation has finished; materialized rows can outlive it. SQL uses Psycopg's native
parameters. This module neither translates SQLite SQL nor performs external work.
"""

import hashlib
import math
import time
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
# The system view also joins every index and groups rows for scan statistics.
# Custody needs only these same continuously updated per-transaction row counts.
TRANSACTION_WRITE_COUNT = (
    "COALESCE((SELECT sum(pg_stat_get_xact_tuples_inserted(c.oid) + "
    "pg_stat_get_xact_tuples_updated(c.oid) + pg_stat_get_xact_tuples_deleted(c.oid)) "
    "FROM pg_catalog.pg_class AS c WHERE c.relnamespace=current_schema()::regnamespace "
    "AND c.relkind IN ('r','t','m','p') "
    "AND current_schema() NOT IN ('pg_catalog','information_schema') "
    "AND current_schema() !~ '^pg_toast'),0)::text"
)


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
        self._deadline = None
        self._timeout_limit = 0

    def _execute(self, cursor, statement, parameters=(), *, timeout_ms=None):
        if self._deadline is not None:
            remaining = math.ceil((self._deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise psycopg.errors.QueryCanceled("local SQL deadline exceeded")
            if self._timeout_limit:
                remaining = min(remaining, self._timeout_limit)
            if timeout_ms is not None:
                remaining = min(remaining, timeout_ms)
            # SET TRANSACTION must precede every SELECT in a fresh transaction.
            if not (isinstance(statement, str) and statement.startswith("SET TRANSACTION ")):
                cursor.execute("SELECT set_config('statement_timeout',%s,false)",
                               (f"{remaining}ms",))
        cursor.execute(statement, parameters)

    @contextmanager
    def deadline(self, seconds):
        """Apply one absolute operation deadline to each native SQL statement.

        An inner scope cannot extend its parent or an existing server timeout.
        Transaction owners still roll back cancellation; no query or effect is retried here.
        """
        if seconds <= 0:
            raise psycopg.errors.QueryCanceled("local SQL deadline exceeded")
        previous_deadline, previous_limit = self._deadline, self._timeout_limit
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT setting FROM pg_settings WHERE name='statement_timeout'")
            previous = int(cursor.fetchone()["setting"])
        self._deadline = min(previous_deadline or float("inf"), time.monotonic() + seconds)
        self._timeout_limit = previous
        try:
            yield
        finally:
            self._deadline, self._timeout_limit = previous_deadline, previous_limit
            # An aborted enclosing transaction must roll back before further SQL;
            # that rollback also restores any timeout set within the transaction.
            if self._connection.info.transaction_status != TransactionStatus.INERROR:
                with self._connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('statement_timeout',%s,false)",
                                   (f"{previous}ms",))

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
            self._execute(cursor, statement, parameters)
            return cursor.rowcount

    def one(self, statement, parameters=()):
        if self._closed:
            raise RuntimeError("POSTGRES_SESSION_CLOSED")
        with self._connection.cursor() as cursor:
            self._execute(cursor, statement, parameters)
            return cursor.fetchone()

    def rows(self, statement, parameters=()):
        if self._closed:
            raise RuntimeError("POSTGRES_SESSION_CLOSED")
        with self._connection.cursor() as cursor:
            self._execute(cursor, statement, parameters)
            return cursor.fetchall()

    def bounded_read(self, statement, parameters, *, limit, timeout_ms, readonly=True):
        """Materialize a finite optional SELECT without aborting its caller's snapshot.

        Psycopg creates a savepoint inside an existing transaction. Read-only mode
        refuses data-changing CTEs; cancellation rolls back only this optional read.
        """
        if self._closed:
            raise RuntimeError("POSTGRES_SESSION_CLOSED")
        if type(limit) is not int or limit < 1 or type(timeout_ms) is not int or timeout_ms < 1:
            raise ValueError("invalid bounded read limits")
        active = self.in_transaction
        scope = self._connection.transaction() if active else self.transaction(readonly=True)
        with scope:
            settings = self.one(
                "SELECT current_setting('transaction_read_only') AS readonly, "
                "(EXTRACT(EPOCH FROM current_setting('statement_timeout')::interval)*1000)"
                "::bigint AS timeout"
            )
            if readonly and settings["readonly"] != "on":
                raise RuntimeError("BOUNDED_READ_REQUIRES_READ_ONLY_SNAPSHOT")
            timeout_ms = min(timeout_ms, settings["timeout"] or timeout_ms)
            with psycopg.ClientCursor(self._connection) as cursor:
                # Psycopg quotes every value and sends these native statements in
                # one simple-Query message. A failed SELECT still rolls back this
                # savepoint. Re-read the live timeout so the absolute deadline set
                # by _execute cannot be widened by the first statement.
                self._execute(cursor, sql.SQL(
                    "SELECT set_config('statement_timeout',LEAST(%s,COALESCE(NULLIF("
                    "(EXTRACT(EPOCH FROM current_setting('statement_timeout')::interval)*1000)"
                    "::bigint,0),%s))::text||'ms',true);"
                    "SELECT * FROM ({}) AS bounded_result LIMIT %s;"
                    "SELECT set_config('statement_timeout',%s,true)").format(
                        statement if isinstance(statement, sql.Composable) else sql.SQL(statement)),
                    (timeout_ms, timeout_ms, *parameters, limit, f"{settings['timeout']}ms"),
                    timeout_ms=timeout_ms)
                cursor.nextset()
                result = cursor.fetchmany(limit)
                cursor.nextset()
            return result

    @contextmanager
    def transaction(self, *, readonly=False, rollback=False):
        if self._closed:
            raise RuntimeError("POSTGRES_SESSION_CLOSED")
        if self._connection.info.transaction_status != TransactionStatus.IDLE:
            raise RuntimeError("POSTGRES_TRANSACTION_ALREADY_ACTIVE")
        previous = self._connection.isolation_level, self._connection.read_only
        try:
            if readonly:
                # Psycopg includes these public characteristics in native BEGIN,
                # avoiding a separate SET TRANSACTION send for every read scope.
                self._connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                self._connection.read_only = True
            with self._connection.transaction(force_rollback=rollback):
                self._ledger_locks = {}
                try:
                    yield self
                finally:
                    self._ledger_locks = None
        finally:
            if readonly and not self._connection.closed:
                if self._connection.info.transaction_status == TransactionStatus.IDLE:
                    self._connection.isolation_level, self._connection.read_only = previous
                else:
                    # An uncertain teardown must not return temporary BEGIN
                    # characteristics to another pool borrower.
                    self._connection.close()

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
        self.ledger_locks(((domain, shared),))

    def ledger_locks(self, domains):
        """Acquire missing ledger locks in caller order; native locks outlive this call."""
        if self._connection.info.transaction_status != TransactionStatus.INTRANS:
            raise RuntimeError("POSTGRES_TRANSACTION_REQUIRED")
        # A managed session keeps its configured schema for this finite scope.
        # PostgreSQL retains these locks until commit/rollback; an exclusive
        # acquisition also covers later shared requests. Upgrades still acquire.
        known = dict(self._ledger_locks or {})
        pending = []
        for domain, shared in domains:
            if type(domain) is not int or not 1 <= domain <= 16:
                raise ValueError("invalid ledger lock domain")
            held = known.get(domain)
            if held is False or held is True and shared:
                continue
            pending.append((domain, bool(shared)))
            known[domain] = bool(shared)
        if not pending:
            return
        statements = []
        for index, (_, shared) in enumerate(pending):
            function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
            statement = sql.SQL("SELECT {}(hashtext(current_schema()),%s)").format(
                sql.Identifier(function))
            if index:
                statement += sql.SQL(" FROM {} ").format(sql.Identifier(f"ledger_{index - 1}"))
            statements.append(statement)
        if len(statements) == 1:
            query = statements[0]
        else:
            # Each materialized CTE consumes its predecessor before taking the next
            # lock. Data reads deliberately remain a later, separate SQL statement.
            entries = [sql.SQL("{} AS MATERIALIZED ({})").format(
                sql.Identifier(f"ledger_{index}"), statement)
                for index, statement in enumerate(statements)]
            query = (sql.SQL("WITH ") + sql.SQL(", ").join(entries)
                     + sql.SQL(" SELECT * FROM {}").format(
                         sql.Identifier(f"ledger_{len(statements) - 1}")))
        self.execute(query, tuple(domain for domain, _ in pending))
        if self._ledger_locks is not None:
            self._ledger_locks.update(known)
