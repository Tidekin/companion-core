"""Native PostgreSQL concurrency and ownership checks against an isolated service."""

import json
import os
import re
import secrets
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr

from companion.postgres import BUDGET_LEDGER, WAKE_CHANNEL, Database


@pytest.fixture
def database():
    configuration = os.environ.get("COMPANION_POSTGRES_TEST_CONFIG")
    if not configuration:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("native PostgreSQL test service is mandatory in CI")
        pytest.skip("isolated PostgreSQL integration service is not configured")
    value = json.loads(Path(configuration).read_text("utf8"))
    connection = {key: value[key] for key in ["host", "port", "dbname", "user", "password"]}
    namespace = "pytest_" + uuid4().hex
    with psycopg.connect(**connection, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
    info = SecretStr(make_conninfo(**connection, options=f"-c search_path={namespace},pg_catalog"))
    try:
        with Database(info, max_connections=2) as result:
            with result.session() as session:
                session.execute("CREATE TABLE items (id bigint PRIMARY KEY, value bigint NOT NULL)")
                session.execute("INSERT INTO items VALUES (1, 0), (2, 0)")
            yield result
    finally:
        assert re.fullmatch(r"pytest_[a-f0-9]{32}", namespace)
        with psycopg.connect(**connection, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace)))


def test_unrelated_row_writes_proceed_while_another_transaction_is_open(database):
    with database.session() as first, database.session() as second:
        first.execute("SET statement_timeout='2s'")
        second.execute("SET statement_timeout='2s'")
        assert first.one("SELECT pg_backend_pid() AS pid") != second.one(
            "SELECT pg_backend_pid() AS pid")
        with first.transaction():
            first.execute("UPDATE items SET value=1 WHERE id=1")
            with second.transaction():
                second.execute("UPDATE items SET value=2 WHERE id=2")
            assert first.one("SELECT value FROM items WHERE id=2") == {"value": 2}
        assert second.rows("SELECT id,value FROM items ORDER BY id") == [
            {"id": 1, "value": 1}, {"id": 2, "value": 2}]


def test_repeatable_read_snapshot_is_consistent_and_cannot_write(database):
    with database.session() as first, database.session() as second:
        with first.transaction(readonly=True):
            assert first.one("SELECT value FROM items WHERE id=1") == {"value": 0}
            second.execute("UPDATE items SET value=2 WHERE id=1")
            assert first.one("SELECT value FROM items WHERE id=1") == {"value": 0}
        assert first.one("SELECT value FROM items WHERE id=1") == {"value": 2}
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction), first.transaction(readonly=True):
            first.execute("UPDATE items SET value=3 WHERE id=1")
        assert first.one("SELECT value FROM items WHERE id=1") == {"value": 2}


def test_custody_lock_is_transaction_scoped_without_locking_other_rows(database):
    key = secrets.randbelow(2**63)
    with database.session() as first, database.session() as second:
        with first.transaction():
            first.custody_lock(key)
            with second.transaction():
                assert second.one("SELECT pg_try_advisory_xact_lock(%s) AS held", (key,)) == {
                    "held": False}
                second.execute("UPDATE items SET value=1 WHERE id=2")
        with second.transaction():
            assert second.one("SELECT pg_try_advisory_xact_lock(%s) AS held", (key,)) == {
                "held": True}


def test_repeated_ledger_locks_upgrade_and_reacquire_after_rollback(database):
    def available(session, *, shared=False):
        function = "pg_try_advisory_xact_lock_shared" if shared else "pg_try_advisory_xact_lock"
        with session.transaction():
            return session.one(sql.SQL("SELECT {}(hashtext(current_schema()),%s) AS held").format(
                sql.Identifier(function)), (BUDGET_LEDGER,))["held"]

    with database.session() as first, database.session() as second:
        with first.transaction(rollback=True):
            first.ledger_lock(BUDGET_LEDGER, shared=True)
            first.ledger_lock(BUDGET_LEDGER, shared=True)
            assert available(second, shared=True)
            assert not available(second)
            first.ledger_lock(BUDGET_LEDGER)
            first.ledger_lock(BUDGET_LEDGER, shared=True)
            assert not available(second, shared=True)
        assert available(second)
        with first.transaction():
            first.ledger_lock(BUDGET_LEDGER)
            assert not available(second)
        assert available(second)


def test_rolled_back_transaction_does_not_publish_wakeup(database):
    with database.listener() as listener, database.session() as session:
        with pytest.raises(ValueError), session.transaction():
            session.execute("UPDATE items SET value=3 WHERE id=1")
            session.notify()
            raise ValueError("synthetic rollback")
        assert list(listener.notifies(timeout=.1, stop_after=1)) == []
        with session.transaction():
            session.execute("UPDATE items SET value=2 WHERE id=1")
            session.notify()
        notifications = list(listener.notifies(timeout=3, stop_after=1))
        assert [(value.channel, value.payload) for value in notifications] == [(WAKE_CHANNEL, "")]
        assert session.one("SELECT value FROM items WHERE id=1") == {"value": 2}


def test_unfinished_transaction_is_rolled_back_before_pool_return(database):
    with database.session() as session:
        session.execute("BEGIN")
        session.execute("UPDATE items SET value=9 WHERE id=1")
    with pytest.raises(RuntimeError, match="POSTGRES_SESSION_CLOSED"):
        session.one("SELECT value FROM items WHERE id=1")
    with database.session() as successor:
        assert successor.one("SELECT value FROM items WHERE id=1") == {"value": 0}


def test_transaction_primitives_require_a_transaction(database):
    with database.session() as session:
        with pytest.raises(RuntimeError, match="POSTGRES_TRANSACTION_REQUIRED"):
            session.custody_lock(1)
        with pytest.raises(RuntimeError, match="POSTGRES_TRANSACTION_REQUIRED"):
            session.notify()
        with session.transaction():
            with pytest.raises(RuntimeError, match="POSTGRES_TRANSACTION_ALREADY_ACTIVE"):
                with session.transaction():
                    pytest.fail("nested transaction was incorrectly admitted")


def test_skip_locked_claims_distinct_rows_and_rollback_releases_claim(database):
    claim = "SELECT id FROM items ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
    with database.session() as first, database.session() as second:
        with pytest.raises(ValueError), first.transaction():
            assert first.one(claim) == {"id": 1}
            with second.transaction():
                assert second.one(claim) == {"id": 2}
            raise ValueError("synthetic claimant exits before commit")
        with second.transaction():
            assert second.one(claim) == {"id": 1}


def test_database_requires_secret_connection_information():
    with pytest.raises(TypeError, match="SecretStr"):
        Database("synthetic-plain-connection-info")


def test_only_materialized_results_outlive_their_session(database):
    with database.session() as session:
        one = session.one("SELECT id,value FROM items WHERE id=1")
        rows = session.rows("SELECT id,value FROM items ORDER BY id")
        changed = session.execute("UPDATE items SET value=4 WHERE id=1")
    with database.session() as successor, successor.transaction():
        successor.execute("UPDATE items SET value=5 WHERE id=1")
        assert one == {"id": 1, "value": 0}
        assert rows == [{"id": 1, "value": 0}, {"id": 2, "value": 0}]
        assert type(changed) is int and changed == 1
        with pytest.raises(RuntimeError, match="POSTGRES_SESSION_CLOSED"):
            session.execute("UPDATE items SET value=6 WHERE id=1")


def test_bounded_read_retains_timeout_and_snapshot_after_optional_error(database):
    with database.session() as reader, database.session() as writer:
        reader.execute("SET statement_timeout='3s'")
        with reader.transaction(readonly=True):
            assert reader.one("SELECT value FROM items WHERE id=1") == {"value": 0}
            writer.execute("UPDATE items SET value=7 WHERE id=1")
            for statement, problem in (
                ("SELECT pg_sleep(1)", psycopg.errors.QueryCanceled),
                ("SELECT * FROM absent_bounded_table", psycopg.errors.UndefinedTable),
                ("SELECT nextval('absent_sequence')", psycopg.errors.UndefinedTable),
            ):
                with pytest.raises(problem):
                    reader.bounded_read(statement, (), limit=1, timeout_ms=5)
                assert reader.one("SHOW statement_timeout") == {"statement_timeout": "3s"}
                assert reader.one("SELECT value FROM items WHERE id=1") == {"value": 0}
            assert reader.bounded_read("SELECT id,value FROM items ORDER BY id", (),
                                       limit=1, timeout_ms=50) == [{"id": 1, "value": 0}]
            assert reader.one("SHOW statement_timeout") == {"statement_timeout": "3s"}
        assert reader.one("SELECT value FROM items WHERE id=1") == {"value": 7}
        assert reader.one("SHOW statement_timeout") == {"statement_timeout": "3s"}


@pytest.mark.parametrize("active", [False, True])
def test_bounded_read_cannot_extend_an_existing_shorter_server_timeout(database, active):
    with database.session() as reader:
        reader.execute("SET statement_timeout='20ms'")
        with reader.transaction(readonly=True) if active else nullcontext():
            with pytest.raises(psycopg.errors.QueryCanceled):
                reader.bounded_read("SELECT pg_sleep(.1)", (), limit=1, timeout_ms=500)
            assert reader.one("SHOW statement_timeout") == {"statement_timeout": "20ms"}
            assert reader.one("SELECT 1 AS ready") == {"ready": 1}
