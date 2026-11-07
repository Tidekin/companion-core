"""Core transaction ownership and concurrency over the real isolated PostgreSQL service."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from queue import Queue
from time import monotonic, sleep

import pytest

from companion.postgres_store import BUDGET_LEDGER, Store
from tests.test_postgres_schema import isolated_native


@pytest.fixture
def database():
    with isolated_native() as (database, _):
        with database.session() as session:
            session.execute("INSERT INTO runtime_meta (key,value) VALUES ('privacy_epoch','0')")
            session.execute("CREATE TABLE items (id bigint PRIMARY KEY,value bigint NOT NULL)")
            session.execute("INSERT INTO items VALUES (1,0),(2,0)")
        yield database


def test_read_snapshot_promotes_only_after_readonly_scope_is_closed(database, tmp_path):
    with closing(Store(database, tmp_path)) as store:
        attempts = []

        def operation():
            attempts.append((store.in_read_snapshot, store.in_write_transaction))
            store.execute("UPDATE items SET value=value+1 WHERE id=1")
            return store.one("SELECT value FROM items WHERE id=1")

        assert store.read_or_write(operation) == {"value": 1}
        assert attempts == [(True, False), (False, True)]
        assert not store.in_transaction


def test_ordinary_transactions_do_not_serialize_unrelated_writes(database, tmp_path):
    with closing(Store(database, tmp_path)) as first, closing(Store(database, tmp_path)) as second:
        with first.transaction():
            first.execute("UPDATE items SET value=1 WHERE id=1")
            with second.transaction():
                second.execute("SET LOCAL statement_timeout='2s'")
                second.execute("UPDATE items SET value=2 WHERE id=2")
            assert first.one("SELECT value FROM items WHERE id=2") == {"value": 2}


def test_budget_stop_and_outer_scope_ownership_survive_failure(database, tmp_path):
    with closing(Store(database, tmp_path)) as store:
        with pytest.raises(ValueError, match="synthetic business failure"), store.transaction():
            store.ledger_lock(BUDGET_LEDGER)
            store.execute("UPDATE items SET value=3 WHERE id=1")
            store.retain_budget_stop("budget-original", "a" * 64)
            with pytest.raises(RuntimeError, match="POSTGRES_TRANSACTION_ALREADY_ACTIVE"):
                with store.transaction():
                    pytest.fail("nested transaction was admitted")
            assert store.in_write_transaction
            with pytest.raises(RuntimeError, match="POSTGRES_STORE_SCOPE_ACTIVE"):
                store.close()
            raise ValueError("synthetic business failure")
        assert store.one("SELECT value FROM items WHERE id=1") == {"value": 0}
        assert store.one("SELECT value FROM runtime_meta WHERE key=%s", (
            "model_budget_stop:" + "a" * 64,)) == {"value": "budget-original"}


def test_snapshot_token_observes_actual_cte_writes_and_pins_read_only_view(database, tmp_path):
    with closing(Store(database, tmp_path)) as first, closing(Store(database, tmp_path)) as second:
        before = first.change_token()
        with second.transaction():
            second.execute("UPDATE items SET value=1 WHERE id=2")
        assert first.change_token()["snapshot"] != before["snapshot"]
        with first.transaction():
            previous = int(first.change_token()["writes"])
            first.one("WITH changed AS (UPDATE items SET value=1 WHERE id=1 RETURNING id) "
                      "SELECT count(*) AS n FROM changed")
            assert int(first.change_token()["writes"]) > previous

        def operation():
            token = first.change_token()
            scope = first.frozen_scope
            with second.transaction():
                second.execute("UPDATE items SET value=2 WHERE id=2")
            assert first.change_token() == token
            assert first.frozen_scope == scope
            assert first.one("SELECT value FROM items WHERE id=2") == {"value": 1}

        first.read_or_write(operation)


def test_control_change_waits_for_existing_transaction(database, tmp_path):
    identity = Queue()

    def control_change():
        with closing(Store(database, tmp_path)) as control:
            identity.put(control.one("SELECT pg_backend_pid() AS pid")["pid"])
            with control.transaction(control_write=True):
                assert control.control_write
                control.execute("UPDATE runtime_meta SET value='1' WHERE key='privacy_epoch'")

    with (closing(Store(database, tmp_path)) as reader,
          ThreadPoolExecutor(max_workers=1) as executor):
        with reader.transaction():
            future = executor.submit(control_change)
            pid = identity.get(timeout=5)
            deadline = monotonic() + 2
            while True:
                with database.session() as probe:
                    row = probe.one("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                                    (pid,))
                if row == {"wait_event_type": "Lock"}:
                    break
                assert monotonic() < deadline, "control transaction did not reach the native lock"
                sleep(.01)
            assert not future.done()
        future.result(timeout=5)
        assert reader.one("SELECT value FROM runtime_meta WHERE key='privacy_epoch'") == {
            "value": "1"}


def test_capability_check_uses_native_columns_and_content_constraint(database, tmp_path):
    with closing(Store(database, tmp_path)) as store:
        assert store.schema_capabilities()
        with store.transaction():
            store.execute("ALTER TABLE content_items DROP COLUMN slot CASCADE")
        assert not store.schema_capabilities()
