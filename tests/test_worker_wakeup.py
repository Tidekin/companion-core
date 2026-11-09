"""Cross-connection wake hints, native deadline clocks and cancellation ownership."""

import asyncio
from contextlib import closing
from datetime import UTC, datetime, timedelta
from time import monotonic

import pytest

from companion import worker_deadlines, worker_wakeup
from companion.postgres import Database
from companion.store import Store, after, utcnow
from companion.worker_loop import Recorder, project_public, snapshot
from companion.worker_schedule import UnitResult
from companion.worker_wakeup import Maintenance, Wakeup
from tests.conftest import ingest, run
from tests.postgres_fixture import logical_state


@pytest.mark.parametrize("kind", list(worker_deadlines.PROVIDERS))
def test_each_worker_can_subscribe_scan_and_wake_on_foreign_commit(core, kind):
    async def scenario():
        with Wakeup(core.store, core.settings, kind, utcnow) as wake:
            scan = wake.before_scan()
            pending = asyncio.create_task(wake.wait(scan, UnitResult(False)))
            await asyncio.sleep(0.04)
            with closing(Store(core.store.database, core.store.state_directory)) as other:
                with other.transaction():
                    other.execute(
                        "INSERT INTO runtime_meta (key,value) VALUES ('foreign-write','1')"
                    )
            assert await asyncio.wait_for(pending, 2) == "commit"
    run(scenario())


@pytest.mark.parametrize("timing", ["after_subscribe", "after_snapshot", "after_scan"])
def test_commit_around_subscription_and_scan_is_not_lost(core, timing):
    async def scenario():
        with Wakeup(core.store, core.settings, "worker", utcnow) as wake:
            with closing(Store(core.store.database, core.store.state_directory)) as other:
                if timing == "after_subscribe":
                    with other.transaction():
                        other.execute("INSERT INTO runtime_meta (key,value) VALUES ('race','1')")
                scan = wake.before_scan()
                if timing == "after_subscribe":
                    assert core.store.one("SELECT value FROM runtime_meta WHERE key='race'")
                    return
                if timing == "after_snapshot":
                    with other.transaction():
                        other.execute("INSERT INTO runtime_meta (key,value) VALUES ('race','1')")
                core.store.one("SELECT COUNT(*) FROM jobs")
                if timing == "after_scan":
                    with other.transaction():
                        other.execute("INSERT INTO runtime_meta (key,value) VALUES ('race','1')")
            assert await asyncio.wait_for(wake.wait(scan, UnitResult(False)), 1) == "commit"
    run(scenario())


def test_recorder_self_writes_do_not_wake_business_scan(core):
    async def scenario():
        with Wakeup(core.store, core.settings, "worker", utcnow) as wake:
            scan = wake.before_scan()
            Recorder(core.store).completed(waiting=True)
            started = monotonic()
            assert await wake.wait(scan, UnitResult(False), started + 0.15) == "maintenance"
            assert monotonic() - started >= 0.14
    run(scenario())


@pytest.mark.parametrize("before_scan_delay", [0, 0.2])
def test_due_retry_wakes_without_a_writer_and_native_claim_succeeds(core, before_scan_delay):
    controlled_clock = core.clock
    core.clock = utcnow
    job = ingest(core)

    async def scenario():
        with Wakeup(core.store, core.settings, "worker", utcnow) as wake:
            controlled_clock.now = datetime.fromisoformat(utcnow())
            core.clock = wake.clock = controlled_clock
            due = after(controlled_clock(), 0.15)
            core.store.execute(
                "UPDATE jobs SET state='RETRY_WAIT',next_attempt_at=%s WHERE job_id=%s",
                (due, job["job_id"]),
            )
            await asyncio.sleep(before_scan_delay)
            scan = wake.before_scan()
            assert core.claim_job("too-early") is None
            core.clock = wake.clock = utcnow
            assert await asyncio.wait_for(wake.wait(scan, UnitResult(False)), 1) == "deadline"
            assert core.claim_job("now-due")["job_id"] == job["job_id"]
    run(scenario())


@pytest.mark.parametrize(("kind", "timeout"), [("worker", 1.0), ("executive-worker", 0.12)])
def test_missed_notification_is_reconciled_without_repeated_business_scans(
    core, monkeypatch, kind, timeout,
):
    monkeypatch.setattr(worker_wakeup, "MAINTENANCE_SECONDS", timeout)

    async def scenario():
        with Wakeup(core.store, core.settings, kind, utcnow) as wake:
            receive = wake._receive

            def lose_hint(delay):
                receive(delay)
                return []

            monkeypatch.setattr(wake, "_receive", lose_hint)
            scan = wake.before_scan()
            pending = asyncio.create_task(wake.wait(scan, UnitResult(False)))
            await asyncio.sleep(0.02)
            with closing(Store(core.store.database, core.store.state_directory)) as other:
                with other.transaction():
                    other.execute("INSERT INTO runtime_meta (key,value) VALUES ('missed','1')")
            assert await asyncio.wait_for(pending, timeout + 1) == "reconcile"
            assert wake.notification_checks <= 2 + timeout / worker_wakeup.LISTENER_WAIT_SECONDS
    run(scenario())


def test_cancel_joins_watch_before_releasing_native_watcher(core):
    async def scenario():
        with Wakeup(core.store, core.settings, "worker", utcnow) as wake:
            listener = wake.listener
            scan = wake.before_scan()
            pending = asyncio.create_task(wake.wait(scan, UnitResult(False)))
            await asyncio.sleep(0.03)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 1)
        assert listener.closed
    run(scenario())


def test_wall_clock_rollback_rechecks_without_stale_deadline(core, monkeypatch):
    monkeypatch.setattr(worker_wakeup, "MAINTENANCE_SECONDS", 0.1)
    now = [utcnow()]

    async def scenario():
        with Wakeup(core.store, core.settings, "worker", lambda: now[0]) as wake:
            scan = wake.before_scan()
            pending = asyncio.create_task(wake.wait(scan, UnitResult(False)))
            await asyncio.sleep(0.03)
            now[0] = after(now[0], -100)
            assert await asyncio.wait_for(pending, 1) == "clock"
    run(scenario())


def test_maintenance_is_elapsed_time_not_notification_count(monkeypatch):
    ticks = [10.0]
    monkeypatch.setattr(worker_wakeup, "monotonic", lambda: ticks[0])
    timer = Maintenance()
    assert timer.due()
    timer.completed()
    for _ in range(1000):
        assert not timer.due()
    ticks[0] += 60
    assert timer.due()


def test_wait_record_is_not_health_and_expires_under_strict_public_projection(core):
    recorder = Recorder(core.store)
    recorder.completed(waiting=True)
    now = datetime.fromisoformat(utcnow())
    fresh = snapshot(core.store, now)
    assert fresh["state"] == "WAITING" and fresh["stage"] == "WAITING_AFTER_POLL"
    assert project_public(fresh, now) == fresh
    stale = snapshot(core.store, datetime.fromisoformat(after(now.isoformat(), 11)))
    assert stale["state"] == "STALE"
    with pytest.raises(ValueError):
        project_public({**fresh, "state": "HEALTHY"}, now)


def test_all_deadline_queries_use_existing_schema(core):
    for kind in worker_deadlines.PROVIDERS:
        assert worker_deadlines.PROVIDERS[kind](
            core.store, core.settings, datetime.fromisoformat(utcnow()),
        )


@pytest.mark.parametrize("kind,table,field,state", [
    ("content-worker", "content_items", "lease_until", "FETCHING"),
    ("executive-worker", "tool_tasks", "lease_until", "LEASED"),
])
@pytest.mark.parametrize("crossed_during_scan", [True, False])
def test_scan_deadline_race_rescans_once_without_spinning_on_fenced_work(
    core, monkeypatch, kind, table, field, state, crossed_during_scan, empty_database,
):
    # Isolate the actual deadline SQL and wait loop from unrelated fixture clocks,
    # filesystem events and provider work. Both workers wait without maintenance_at.
    start = datetime(2026, 10, 24, 12, tzinfo=UTC)
    elapsed = [0.0]
    monkeypatch.setattr(worker_wakeup, "monotonic", lambda: elapsed[0])

    def clock():
        return (start + timedelta(seconds=elapsed[0])).isoformat()

    with (Database(empty_database) as database,
          closing(Store(database, core.store.state_directory)) as store):
        store.execute("CREATE TABLE runtime_meta (key TEXT PRIMARY KEY,value TEXT)")
        fields = {"lease_until", "deadline"} if kind == "content-worker" else {"lease_until"}
        store.execute(f"CREATE TABLE {table} (state TEXT, "
                      + ", ".join(f"{column} TEXT" for column in fields) + ")")
        store.execute("CREATE TABLE model_admissions (request_id TEXT,lease_until TEXT)")
        store.execute("CREATE TABLE model_usage (request_id TEXT,state TEXT)")
        due = start + timedelta(seconds=0.1 if crossed_during_scan else -1)
        store.execute(f"INSERT INTO {table} (state,{field}) VALUES (%s,%s)",
                      (state, due.isoformat()))
        wake = Wakeup(store, core.settings, kind, clock)

        waits = []

        def receive(delay):
            if delay:
                waits.append(delay)
                elapsed[0] += delay
            return []

        monkeypatch.setattr(wake, "_receive", receive)
        async def scenario():
            scan = wake.before_scan()
            # Native recovery sees no eligible lease before the boundary.
            if crossed_during_scan:
                assert store.one(f"SELECT state FROM {table} WHERE {field}<=%s",
                                 (clock(),)) is None
            elapsed[0] = 0.2
            if crossed_during_scan:
                assert await wake.wait(scan, UnitResult(False)) == "deadline"
                assert not waits
                assert store.one(f"SELECT state FROM {table} WHERE {field}<=%s", (clock(),))
                # A subsequent scan can leave this row fenced. It must not cause
                # repeated immediate wakeups simply because its lease is past due.
                scan = wake.before_scan()
            assert await wake.wait(scan, UnitResult(False)) == "reconcile"
            assert sum(waits) >= worker_wakeup.MAINTENANCE_SECONDS
            assert all(delay > 0 for delay in waits)


        run(scenario())


def test_wakeup_listens_on_a_dedicated_postgres_session(core):
    from companion.postgres import WAKE_CHANNEL

    with Wakeup(core.store, core.settings, "content-worker", utcnow) as wake:
        assert wake.listener.info.backend_pid != core.store.backend_pid
        assert wake.listener.execute(
            "SELECT pg_listening_channels() AS channel"
        ).fetchall() == [(WAKE_CHANNEL,)]
    assert wake.listener.closed


def test_waiting_record_cannot_prove_activity_or_console_health(core):
    from companion import console_read, observe

    core.clock = utcnow
    Recorder(core.store).completed(waiting=True)
    value = console_read.overview(core)
    assert value["runtime"]["worker"]["state"] == "WAITING"
    assert value["runtime"]["state"] == "ATTENTION"
    activity = observe._LoopActivity().sample({
        "worker_loop": value["runtime"]["worker"], "observed_at": value["observed_at"],
    }, 0, [])
    assert activity["state"] == "UNPROVEN"
    assert activity["reasons"] == ["LOOP_WAITING_UNVERIFIED"]


def test_waiting_chat_job_bounds_chat_rescan_to_former_polling_cadence(core, monkeypatch):
    start = datetime.fromisoformat(core.clock())
    idle = worker_deadlines.chat(core.store, core.settings, start)
    assert idle > start + timedelta(seconds=worker_deadlines.CHAT_WAIT_RESCAN_SECONDS)
    ingest(core)
    # A short wait (here a pending meme offer) leaves the due job unclaimed and
    # writes nothing when it expires; the chat worker must still look again soon.
    monkeypatch.setattr(type(core.memes), "waiting_offer", lambda *_: True)
    assert run(core.run_once("wait-test")) is False
    job = core.store.one("SELECT state FROM jobs WHERE kind='chat'")
    assert job["state"] == "QUEUED"
    before = logical_state(core.store)
    deadline = worker_deadlines.chat(core.store, core.settings, start)
    assert deadline == start + timedelta(seconds=worker_deadlines.CHAT_WAIT_RESCAN_SECONDS)
    assert logical_state(core.store) == before
    future = start + timedelta(hours=1)
    core.store.execute("UPDATE jobs SET next_attempt_at=%s WHERE kind='chat'",
                       (future.isoformat(),))
    # _next adds 2 ms so a crossed boundary rescans after, not before, it.
    assert worker_deadlines.chat(core.store, core.settings, start) <= future + timedelta(
        milliseconds=10)
    assert worker_deadlines.chat(core.store, core.settings, start) > (
        start + timedelta(seconds=worker_deadlines.CHAT_WAIT_RESCAN_SECONDS))


def test_the_meme_offer_wait_rescans_when_it_ends(core):
    from companion.meme_core import OFFER_WAIT_SECONDS

    start = datetime.fromisoformat(core.clock())
    ingest(core)
    created = datetime.fromisoformat(
        core.store.one("SELECT created_at FROM jobs WHERE kind='chat'")["created_at"])
    ends = created + timedelta(seconds=OFFER_WAIT_SECONDS)
    late = ends - timedelta(milliseconds=500)
    deadline = worker_deadlines.chat(core.store, core.settings, late)
    assert ends <= deadline <= ends + timedelta(milliseconds=10)
    # After the wait ends only the ordinary rescan cadence remains.
    after_wait = ends + timedelta(milliseconds=100)
    assert worker_deadlines.chat(core.store, core.settings, after_wait) == after_wait + timedelta(
        seconds=worker_deadlines.CHAT_WAIT_RESCAN_SECONDS)
    assert start <= created
