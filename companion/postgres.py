"""Native PostgreSQL sessions, transaction locks and non-authoritative wake hints.

The database owns a bounded pool. A session owns one connection until its finite
operation has finished; materialized rows can outlive it. SQL uses Psycopg's native
parameters. This module neither translates SQLite SQL nor performs external work.
"""

from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import SecretStr

WAKE_CHANNEL = "companion_wakeup"


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

    def execute(self, statement, parameters=()):
        if self._closed:
            raise RuntimeError("POSTGRES_SESSION_CLOSED")
        return self._connection.execute(statement, parameters)

    def one(self, statement, parameters=()):
        return self.execute(statement, parameters).fetchone()

    def rows(self, statement, parameters=()):
        return self.execute(statement, parameters).fetchall()

    @contextmanager
    def transaction(self, *, readonly=False):
        if self._closed:
            raise RuntimeError("POSTGRES_SESSION_CLOSED")
        if self._connection.info.transaction_status != TransactionStatus.IDLE:
            raise RuntimeError("POSTGRES_TRANSACTION_ALREADY_ACTIVE")
        with self._connection.transaction():
            if readonly:
                self.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            yield self

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
