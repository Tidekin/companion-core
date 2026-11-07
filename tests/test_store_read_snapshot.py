"""Read-only polls use a WAL snapshot; anything else is decided under the writer lock."""

import asyncio
import sqlite3
from contextlib import closing
from time import perf_counter

import httpx
import pytest

from companion import paid_budget, runtime_config
from companion.autonomous_selector import native_selector
from companion.store import Store
from tests.support.api_store import PREFIX, headers
from tests.support.core_http import real_core_http
from tests.support.owner_input import captured


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "snapshot.db"
    instance = Store(str(path))
    instance.execute("CREATE TABLE t(a)")
    try:
        yield instance
    finally:
        instance.close()


def held_writer(path):
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO t VALUES (99)")
    return writer


def test_read_does_not_wait_for_the_writer_and_sees_committed_state(store):
    store.execute("INSERT INTO t VALUES (1)")
    with closing(held_writer(store.path)) as writer:
        store.db.execute("PRAGMA busy_timeout=0")
        before = perf_counter()
        rows = store.read_or_write(lambda: store.rows("SELECT a FROM t"))
        assert perf_counter() - before < 1
        assert rows == [{"a": 1}]  # The uncommitted row is not part of any snapshot.
        assert not store.db.in_transaction
        writer.rollback()


def test_write_attempt_repeats_once_under_the_lock_and_commits(store):
    calls = []

    def operation():
        calls.append(store.in_read_snapshot)
        store.execute("INSERT INTO t VALUES (2)")
        return "written"

    assert store.read_or_write(operation) == "written"
    assert calls == [True, False]
    assert store.rows("SELECT a FROM t") == [{"a": 2}]
    store.execute("INSERT INTO t VALUES (3)")  # query_only is off again.


def test_snapshot_failure_is_decided_again_and_locked_error_is_authoritative(store):
    calls = []

    def flaky():
        calls.append(store.in_write_transaction)
        if len(calls) == 1:
            raise ValueError("journal ahead of snapshot")
        return "locked"

    assert store.read_or_write(flaky) == "locked"
    assert calls == [False, True]

    def refused():
        raise LookupError("denied" if store.in_write_transaction else "snapshot")

    with pytest.raises(LookupError, match="denied"):
        store.read_or_write(refused)


def test_inside_an_open_transaction_runs_in_place(store):
    with store.transaction():
        assert store.read_or_write(lambda: store.in_write_transaction) is True
        store.execute("INSERT INTO t VALUES (4)")
    assert store.rows("SELECT a FROM t") == [{"a": 4}]


def test_shared_journal_view_ignores_snapshots_older_than_the_files(core, tmp_path):
    store = core.store
    with closing(sqlite3.connect(store.path, isolation_level=None)) as other:
        def key():
            local_key = paid_budget._view_key(store, tmp_path / "journal", b"{}\n")
            return paid_budget._shared_key(store, local_key)

        assert store.read_or_write(key) is not None
        stale = []

        def commit_during_snapshot():
            store.one("SELECT 1 FROM runtime_meta LIMIT 1")  # pins the snapshot
            other.execute("INSERT INTO runtime_meta(key,value) VALUES ('snapshot_test','5')")
            stale.append(key())
            raise RuntimeError("retry")

        store.read_or_write(lambda: (commit_during_snapshot()
                                     if not store.in_write_transaction else None))
        assert stale == [None]


def test_owner_check_answers_while_another_connection_holds_the_writer(core, tmp_path):
    capture, _ = captured(core, tmp_path)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, headers=headers(core),
        ) as client:
            with closing(sqlite3.connect(core.settings.database, isolation_level=None)) as w:
                w.execute("BEGIN IMMEDIATE")
                before = perf_counter()
                response = await asyncio.wait_for(client.post(
                    PREFIX + "/owner-authority/check",
                    json={"authority": capture.authority, "purpose": "chat"}), 3)
                elapsed = perf_counter() - before
                w.rollback()
            assert response.status_code == 200, response.text
            assert response.json()["approved"] is True
            # The busy timeout is 5 s; a locked read would still be waiting.
            assert elapsed < 2.5

    asyncio.run(scenario())


def test_unchanged_boundary_needs_no_writer_lock(core):
    runtime_config.boundary(core.store, core.settings, core.clock, worker="life-worker")
    with closing(sqlite3.connect(core.settings.database, isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        core.store.db.execute("PRAGMA busy_timeout=0")
        try:
            runtime_config.boundary(core.store, core.settings, core.clock, worker="life-worker")
        finally:
            writer.rollback()
            core.store.db.execute("PRAGMA busy_timeout=5000")
    with closing(sqlite3.connect(core.settings.database, isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        core.store.db.execute("PRAGMA busy_timeout=0")
        try:
            # A new worker identity must be published, which still takes the lock.
            with pytest.raises(sqlite3.OperationalError):
                runtime_config.boundary(core.store, core.settings, core.clock, worker="other")
        finally:
            writer.rollback()
            core.store.db.execute("PRAGMA busy_timeout=5000")


def test_maintenance_that_changed_nothing_is_not_progress(core):
    selector = native_selector(core, "life-test")
    retention = next(lane for lane in selector.lanes if lane.name == "retention")
    from tests.conftest import run

    assert run(retention.run()).progressed is False
