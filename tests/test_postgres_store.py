"""Core transaction ownership and concurrency over the real isolated PostgreSQL service."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from queue import Queue
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from companion import paid_budget
from companion.contracts import CoreError
from companion.postgres import BUDGET_LEDGER
from companion.store import Store
from tests.postgres_fixture import isolated_native


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


def test_bootstrap_preserves_imported_voice_identity_and_owner(database, tmp_path):
    with closing(Store(database, tmp_path)) as store:
        incarnation = "1234567890abcdef1234567890abcdef"
        store.execute("INSERT INTO runtime_meta (key,value) VALUES ('voice_incarnation',%s)",
                      (incarnation,))
        for _ in range(2):
            store.bootstrap("10001", "2026-01-01T00:00:00+00:00")
        assert store.one("SELECT value FROM runtime_meta WHERE key='voice_incarnation'") == {
            "value": incarnation}
        assert store.one("SELECT value FROM runtime_meta WHERE key='owner_qq_id'") == {
            "value": "10001"}
        with pytest.raises(CoreError, match="OWNER_IDENTITY_MISMATCH"):
            store.bootstrap("20001", "2026-01-02T00:00:00+00:00")
        assert store.one("SELECT value FROM runtime_meta WHERE key='owner_qq_id'") == {
            "value": "10001"}


def test_paid_custody_cache_detects_peer_and_uncommitted_projection_changes(
    database, tmp_path, monkeypatch,
):
    approval = SimpleNamespace(journal_path=str(tmp_path / "paid.jsonl"))
    original = paid_budget._journal
    folds = []

    def fold(*args, **kwargs):
        folds.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(paid_budget, "_journal", fold)
    with closing(Store(database, tmp_path)) as first, closing(Store(database, tmp_path)) as second:
        first.bootstrap("10001", "2026-01-01T00:00:00+00:00")
        paid_budget.journal(first, approval)

        def repeated_read():
            expected = paid_budget.journal(first, approval)
            before = len(folds)
            assert paid_budget.journal(first, approval) == expected
            assert len(folds) == before
            return expected

        expected = first.read_or_write(repeated_read)
        assert paid_budget.journal(second, approval) == expected
        before = len(folds)
        with second.transaction():
            second.execute("INSERT INTO runtime_meta (key,value) VALUES ('peer-write','changed')")
        assert paid_budget.journal(first, approval) == expected
        assert len(folds) > before
        with first.transaction(), pytest.raises(CoreError, match="RECONCILIATION_REQUIRED"):
            first.execute("UPDATE runtime_meta SET value='tampered' "
                          "WHERE key='model_budget_journal_id'")
            paid_budget.journal(first, approval)


def test_read_snapshot_refuses_journal_append_before_file_effect(database, tmp_path):
    path = tmp_path / "paid.jsonl"
    approval = SimpleNamespace(journal_path=str(path))
    with closing(Store(database, tmp_path)) as store:
        store.bootstrap("10001", "2026-01-01T00:00:00+00:00")
        paid_budget.journal(store, approval)
        original = path.read_bytes()
        attempts = []

        def read():
            attempts.append(store.in_read_snapshot)
            with pytest.raises(RuntimeError, match="MODEL_JOURNAL_REQUIRES_WRITE_TRANSACTION"):
                paid_budget.journal(store, approval, {"kind": "quota_pause"})
            return True

        assert store.read_or_write(read)
        assert attempts == [True]
        assert path.read_bytes() == original


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
