"""Read-only polls observe committed rows while native writers and custody locks are held."""

import asyncio
from contextlib import closing
from time import perf_counter

import httpx
import psycopg
import pytest

from companion import paid_budget, runtime_config
from companion.autonomous_selector import native_selector
from companion.store import Store
from tests.conftest import ingest, run
from tests.support.api_store import PREFIX, headers
from tests.support.core_http import real_core_http
from tests.support.owner_input import captured


@pytest.fixture
def store(core):
    core.store.execute("CREATE TABLE t(a INTEGER)")
    return core.store


def test_read_does_not_wait_for_the_writer_and_sees_committed_state(core, store):
    store.execute("INSERT INTO t VALUES (1)")
    with psycopg.connect(core.settings.database.get_secret_value()) as writer:
        writer.execute("INSERT INTO t VALUES (99)")
        before = perf_counter()
        rows = store.read_or_write(lambda: store.rows("SELECT a FROM t"))
        assert perf_counter() - before < 1
        assert rows == [{"a": 1}]
        assert not store.in_transaction
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
    store.execute("INSERT INTO t VALUES (3)")


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
    def key(raw):
        local = paid_budget._view_key(store, tmp_path / "journal", raw)
        return paid_budget._shared_key(store, local)

    with psycopg.connect(core.settings.database.get_secret_value(), autocommit=True) as writer:
        with store.read_snapshot():
            original = key(b"original")
            assert original is not None
            writer.execute("INSERT INTO runtime_meta(key,value) VALUES ('snapshot_test','5')")
            assert not store.one("SELECT 1 FROM runtime_meta WHERE key='snapshot_test'")
            assert key(b"original") == original
            assert key(b"changed") != original
        with store.read_snapshot():
            assert store.one("SELECT value FROM runtime_meta WHERE key='snapshot_test'") == {
                "value": "5"}
            assert key(b"original") != original
        with store.transaction():
            assert key(b"original") is None


def test_owner_check_answers_while_another_connection_holds_the_writer(core, tmp_path):
    capture, _ = captured(core, tmp_path)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, headers=headers(core),
        ) as client:
            with (closing(Store(core.database, core.settings.state_directory)) as writer,
                  writer.transaction(control_write=True)):
                before = perf_counter()
                response = await asyncio.wait_for(client.post(
                    PREFIX + "/owner-authority/check",
                    json={"authority": capture.authority, "purpose": "chat"}), 3)
                elapsed = perf_counter() - before
            assert response.status_code == 200, response.text
            assert response.json()["approved"] is True
            # The busy timeout is 5 s; a locked read would still be waiting.
            assert elapsed < 2.5

    asyncio.run(scenario())


def test_unchanged_boundary_needs_no_writer_lock(core):
    runtime_config.boundary(core.store, core.settings, core.clock, worker="life-worker")
    with (closing(Store(core.database, core.settings.state_directory)) as writer,
          writer.transaction(control_write=True)):
        runtime_config.boundary(core.store, core.settings, core.clock, worker="life-worker")
        # Publishing a new worker identity must take the privacy row lock.
        with pytest.raises(psycopg.errors.LockNotAvailable):
            runtime_config.boundary(core.store, core.settings, core.clock, worker="other")


def test_retention_reports_only_expired_rows(core):
    selector = native_selector(core, "life-test")
    retention = next(lane for lane in selector.lanes if lane.name == "retention")

    assert run(retention.run()).progressed is False
    event = ingest(core)
    core.clock.advance((core.settings.raw_event_retention_days + 1) * 86400)
    assert run(retention.run()).progressed
    assert core.store.one(
        'SELECT redacted FROM events WHERE event_id=%s', (event['event_id'],),
    )['redacted'] == 1
    assert not run(retention.run()).progressed


def test_retention_reports_rem_expiry_before_raw_cutoff(core, tmp_path):
    from companion import rem
    from tests.support.rem_native import prepared

    async def scenario():
        async with prepared(core, tmp_path, record_retention_days=1):
            assert rem.schedule_due(core)
            retention = next(
                lane for lane in native_selector(core, "retention-test").lanes
                if lane.name == "retention"
            )
            core.clock.advance(2 * 86400)
            assert (await retention.run()).progressed
            assert rem.records(core)[0]["redacted"]
            assert not (await retention.run()).progressed

    run(scenario())
