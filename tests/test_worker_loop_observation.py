"""Loop completion is evidence of scheduling, never business or delivery success."""

import asyncio
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from cryptography.fernet import Fernet

from companion import __main__, observe, ops_status, worker_loop
from companion import core as core_module
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.store import encode
from tests.conftest import ingest, run
from tests.postgres_fixture import isolated_database, logical_state
from tests.support.observations import records, status_server

BASE = datetime(2026, 10, 21, tzinfo=UTC)


def record(sequence=1, *, session="a" * 32, at=0):
    return {"session": session, "sequence": sequence,
            "completed_at": (BASE + timedelta(seconds=at)).isoformat(),
            "stage": "POLL_COMPLETED"}


def sample(sequence=1, *, session="a" * 32, at=0, age=0):
    observed = BASE + timedelta(seconds=at + age)
    return {"observed_at": observed.isoformat(), "blockers": [],
            "worker_loop": worker_loop._project(record(sequence, session=session, at=at), observed)}


def stored(core):
    row = core.store.one('SELECT value FROM runtime_meta WHERE key=%s', (worker_loop.KEY,))
    return json.loads(row["value"]) if row else None


@pytest.fixture
def observation_clock(monkeypatch):
    """Keep observation semantics independent of fsync and scheduler latency."""
    nanos = 0
    anchor = datetime.now(UTC)

    class ObservationTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = anchor + timedelta(microseconds=nanos // 1000)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    def sleep(seconds):
        nonlocal nanos
        nanos += round(seconds * 1e9)

    monkeypatch.setattr(observe, "datetime", ObservationTime)
    monkeypatch.setattr(observe.time, "monotonic_ns", lambda: nanos)
    monkeypatch.setattr(observe.time, "sleep", sleep)


def test_recorder_never_reuses_startup_record_and_throttles_completed_loops(core, monkeypatch):
    prior = encode(record(987))
    core.store.execute('INSERT INTO runtime_meta VALUES (%s,%s)', (worker_loop.KEY, prior))
    ticks = [0.0]
    monkeypatch.setattr(worker_loop, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(worker_loop, "utcnow", core.clock)
    recorder = worker_loop.Recorder(core.store)
    assert stored(core) == json.loads(prior)  # Constructing a worker proves nothing.
    recorder.completed()
    first = stored(core)
    assert first["session"] != "a" * 32 and first["sequence"] == 1
    ticks[0] = 1.9
    recorder.completed()
    assert stored(core) == first
    ticks[0] = 2
    core.clock.advance(2)
    recorder.completed(maintenance=True)
    final = stored(core)
    assert final["session"] == first["session"] and final["sequence"] == 3
    assert final["stage"] == "POLL_AND_MAINTENANCE_COMPLETED"
    assert worker_loop.snapshot(core.store, datetime.fromisoformat(core.clock()))["state"] == (
        "RECORDED"
    )
    assert datetime.fromisoformat(final["completed_at"]) > datetime.fromisoformat(
        first["completed_at"],
    )


def worker_fixture(core, monkeypatch):
    monkeypatch.setattr(core_module, "Core", lambda *_: core)

    class NoNetworkClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(__main__.httpx, "AsyncClient", lambda **_: NoNetworkClient())
    ticks = [0.0]
    monkeypatch.setattr(worker_loop, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(worker_loop, "utcnow", core.clock)
    original_sleep = asyncio.sleep

    async def fast_sleep(_):
        ticks[0] += 2
        core.clock.advance(2)
        await original_sleep(0)

    async def fake_wait(self, *_):
        await asyncio.sleep(1)
        return "commit"

    monkeypatch.setattr(__main__.Wakeup, "wait", fake_wait)
    monkeypatch.setattr(__main__.asyncio, "sleep", fast_sleep)
    return ticks, original_sleep


def test_actual_worker_idle_loops_advance_only_after_native_maintenance(core, monkeypatch):
    worker_fixture(core, monkeypatch)
    snapshots = []
    original_sleep = asyncio.sleep

    async def stop_after_three(seconds):
        snapshots.append(stored(core))
        if len(snapshots) == 3:
            raise asyncio.CancelledError
        await original_sleep(seconds)

    monkeypatch.setattr(__main__.asyncio, "sleep", stop_after_three)
    with pytest.raises(asyncio.CancelledError):
        run(__main__.worker(core.settings))
    assert [row["sequence"] for row in snapshots] == [2, 2, 2]
    assert snapshots[0]["stage"] == "WAITING_AFTER_POLL"
    assert snapshots[1]["stage"] == "WAITING_AFTER_POLL"
    assert len({row["session"] for row in snapshots}) == 1


def test_actual_worker_blocked_poll_never_advances(core, monkeypatch):
    ticks, original_sleep = worker_fixture(core, monkeypatch)

    async def scenario():
        entered = asyncio.Event()
        never = asyncio.Event()
        polls = 0

        async def poll(_):
            nonlocal polls
            polls += 1
            if polls == 2:
                entered.set()
                await never.wait()
            return False

        monkeypatch.setattr(core, "run_once", poll)
        task = asyncio.create_task(__main__.worker(core.settings))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            before = stored(core)
            assert before is not None and before["sequence"] == 1
            ticks[0] += 100
            core.clock.advance(100)
            await original_sleep(0)
            assert stored(core) == before
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    run(scenario())


def test_actual_worker_failed_maintenance_preserves_previous_loop_record(core, monkeypatch):
    worker_fixture(core, monkeypatch)
    previous = encode(record(987))
    core.store.execute('INSERT INTO runtime_meta VALUES (%s,%s)', (worker_loop.KEY, previous))

    def fail():
        raise RuntimeError("synthetic maintenance failure")

    monkeypatch.setattr(core, "expire_raw_events", fail)
    with pytest.raises(RuntimeError, match="synthetic maintenance failure"):
        run(__main__.worker(core.settings))
    # The worker closes its connection even when maintenance fails.
    with closing(psycopg.connect(core.settings.database.get_secret_value())) as database:
        persisted = database.execute(
            "SELECT value FROM runtime_meta WHERE key=%s", (worker_loop.KEY,),
        ).fetchone()
    assert persisted == (previous,)


def test_native_failed_business_is_not_success_even_when_loop_completes(core, monkeypatch):
    ingest(core)
    core.model.error = CoreError("MODEL_INVALID_RESPONSE", 503)
    assert run(core.run_once("synthetic-failure")) is True
    assert core.store.one("SELECT state FROM jobs")["state"] == "FAILED"
    worker_loop.Recorder(core.store).completed()
    snapshot = ops_status.snapshot(core.settings)
    assert snapshot["jobs"]["states"] == {"FAILED": 1}
    assert snapshot["worker_loop"]["state"] == "RECORDED"
    assert snapshot["worker_progress"] == {
        "state": "UNOBSERVED", "reason": "BUSINESS_PROGRESS_NOT_INFERRED",
    }


def test_status_reads_only_a_bounded_safe_record_without_initializing_or_writing(core, monkeypatch):
    worker_loop.Recorder(core.store).completed()
    before = logical_state(core.store)
    ledger = Path(core.settings.erasure_ledger).read_bytes()
    monkeypatch.setattr(core_module.Core, "__init__", lambda *_: pytest.fail("no Core startup"))
    snapshot = ops_status.snapshot(core.settings)
    assert snapshot["worker_loop"]["state"] == "RECORDED"
    assert logical_state(core.store) == before
    assert Path(core.settings.erasure_ledger).read_bytes() == ledger
    assert core.model.calls == []

    secret = "SYNTHETIC_PRIVATE_STAGE_TOKEN_PATH"
    invalid = record()
    invalid["stage"] = secret
    for raw in (encode(invalid), secret * 1000, "{broken:" + secret):
        core.store.execute('UPDATE runtime_meta SET value=%s WHERE key=%s', (raw, worker_loop.KEY))
        result = ops_status.snapshot(core.settings)
        assert result["worker_loop"] == {"state": "INVALID"}
        assert secret not in encode(result)


@pytest.mark.parametrize("change", ["boolean_sequence", "zero_sequence", "large_sequence",
                                   "session", "stage", "naive_time", "non_utc", "future"])
def test_invalid_persisted_record_cannot_become_activity(core, change):
    value = record()
    if change == "boolean_sequence":
        value["sequence"] = True
    elif change == "zero_sequence":
        value["sequence"] = 0
    elif change == "large_sequence":
        value["sequence"] = 2**63
    elif change in {"session", "stage"}:
        value[change] = "SYNTHETIC_PRIVATE"
    elif change == "naive_time":
        value["completed_at"] = BASE.replace(tzinfo=None).isoformat()
    elif change == "non_utc":
        value["completed_at"] = "2026-10-21T08:00:00+08:00"
    else:
        value["completed_at"] = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
    core.store.execute('INSERT INTO runtime_meta VALUES (%s,%s)', (worker_loop.KEY, encode(value)))
    assert ops_status.snapshot(core.settings)["worker_loop"] == {"state": "INVALID"}


def test_sequence_advancement_is_separate_from_clear_samples_and_business_blockers():
    activity = observe._LoopActivity()
    first = activity.sample(sample(1), 0, [])
    assert first["reasons"] == ["LOOP_BASELINE_REQUIRED"]
    advanced = activity.sample(sample(3, at=5), 5_000_000_000, ["OBSERVED_BLOCKERS"])
    assert advanced["state"] == "ADVANCED" and advanced["interval_seconds"] == 5
    assert activity.summary() == {"advancing_intervals": 1, "advancing_interval_seconds": 5,
                                  "last_segment_observed_seconds": 5}


@pytest.mark.parametrize("current,reason", [
    (sample(1, at=5), "LOOP_SEQUENCE_UNCHANGED"),
    (sample(1, at=5, session="b" * 32), "LOOP_SESSION_CHANGED"),
    (sample(1, at=5, age=11), "LOOP_RECORD_STALE"),
    (sample(3, at=-1), "LOOP_COMPLETION_TIME_NOT_ADVANCED"),
    ({"worker_loop": {"state": "INVALID"}}, "LOOP_RECORD_INVALID"),
    ({"worker_loop": {"state": "UNOBSERVED"}}, "NO_LOOP_RECORD"),
])
def test_same_fresh_record_restart_stale_or_invalid_breaks_activity(current, reason):
    activity = observe._LoopActivity()
    activity.sample(sample(1), 0, [])
    value = activity.sample(current, 5_000_000_000, [])
    assert value["state"] == "UNPROVEN" and value["reasons"] == [reason]
    assert activity.summary()["advancing_intervals"] == 0


def test_regression_and_missing_sample_do_not_join_activity_across_restores_or_gaps():
    activity = observe._LoopActivity()
    activity.sample(sample(10), 0, [])  # A freshly restored record is only a baseline.
    assert activity.sample(sample(10), 5_000_000_000, [])["state"] == "UNPROVEN"
    assert activity.sample(sample(11, at=10), 10_000_000_000, [])["segment"] == 1
    assert activity.sample(sample(2, at=15), 15_000_000_000, [])["reasons"] == [
        "LOOP_SEQUENCE_REGRESSED",
    ]
    assert activity.sample(sample(3, at=20), 20_000_000_000, [])["segment"] == 2
    assert activity.sample(None, 25_000_000_000, ["SAMPLE_FAILED"])["state"] == "UNPROVEN"
    assert activity.sample(sample(4, at=30), 30_000_000_000, [])["reasons"] == [
        "LOOP_BASELINE_REQUIRED",
    ]
    assert activity.sample(sample(5, at=35), 35_000_000_000, [])["segment"] == 3
    assert activity.summary()["last_segment_observed_seconds"] == 5
    assert activity.summary()["advancing_interval_seconds"] == 15


@pytest.mark.parametrize("gap", ["SAMPLE_GAP", "COLLECTION_GAP", "WALL_CLOCK_DISCONTINUITY",
                                "API_INSTANCE_CHANGED"])
def test_observation_discontinuity_requires_a_new_loop_baseline(gap):
    activity = observe._LoopActivity()
    activity.sample(sample(1), 0, [])
    assert activity.sample(sample(2, at=5), 5_000_000_000, [gap])["reasons"] == [gap]
    assert activity.sample(sample(3, at=10), 10_000_000_000, [])["reasons"] == [
        "LOOP_BASELINE_REQUIRED",
    ]


def test_observe_persists_activity_without_promoting_worker_health(tmp_path, observation_clock):
    values = iter([sample(1), sample(2, at=5), sample(2, at=5)])
    output = tmp_path / "loop.jsonl"
    result = observe.observe(lambda: (next(values), None), output,
                             samples=3, interval_seconds=5)
    rows = records(output)
    assert result["worker_health_proven"] is False
    assert result["worker_loop_activity"]["advancing_intervals"] == 1
    assert result["worker_loop_activity"]["last_segment_observed_seconds"] == 0
    assert result["clear_segment_observed_seconds"] == 10
    assert [row["worker_loop_activity"]["state"] for row in rows[1:-1]] == [
        "UNPROVEN", "ADVANCED", "UNPROVEN",
    ]


def test_native_restored_fresh_loop_record_never_proves_a_running_worker(
    core, tmp_path, observation_clock,
):
    worker_loop.Recorder(core.store).completed()
    original = stored(core)
    source_before = logical_state(core.store)
    key = Fernet.generate_key()
    archive = tmp_path / "backup.enc"
    backup(core.settings.database, archive, key)
    with isolated_database() as restored:
        state = tmp_path / "restored-state"
        restore(archive, restored, key, core.settings.erasure_ledger, state_directory=state)
        settings = core.settings.model_copy(update={"database": restored,
                                                    "state_directory": str(state)})
        first = ops_status.snapshot(settings)
        assert first["worker_loop"]["state"] == "RECORDED"
        assert first["worker_loop"]["session"] == original["session"]
        assert first["worker_loop"]["sequence"] == original["sequence"]
        output = tmp_path / "restored-loop.jsonl"
        result = observe.observe(lambda: (ops_status.snapshot(settings), None), output,
                                 samples=2, interval_seconds=5)
        assert result["worker_loop_activity"]["advancing_intervals"] == 0
        assert result["worker_health_proven"] is False
        assert [row["worker_loop_activity"]["reasons"] for row in records(output)[1:-1]] == [
            ["LOOP_BASELINE_REQUIRED"], ["LOOP_SEQUENCE_UNCHANGED"],
        ]
    assert logical_state(core.store) == source_before


@pytest.mark.parametrize("change", ["sequence", "age", "state", "state_type", "stage",
                                   "extra", "missing"])
def test_http_loop_fields_are_strictly_projected_and_do_not_persist_private_values(core, change):
    worker_loop.Recorder(core.store).completed()
    marker = "SYNTHETIC_PRIVATE_RECORD"

    def mutate(snapshot):
        value = snapshot["worker_loop"]
        if change == "sequence":
            value["sequence"] = True
        elif change == "age":
            value["age_seconds"] = float("inf")
        elif change == "state":
            value["state"] = "STALE"
        elif change == "state_type":
            value["state"] = [marker]
        elif change == "stage":
            value["stage"] = marker
        elif change == "extra":
            value["private_detail"] = marker
        else:
            del value["completed_at"]

    with status_server(core, mutate=mutate) as (url, _, _):
        with pytest.raises((ValueError, KeyError)):
            observe.http_collector(core.settings, url)()


def test_real_http_observation_records_safe_loop_metadata_only(core, tmp_path):
    worker_loop.Recorder(core.store).completed()
    output = tmp_path / "http-loop.jsonl"
    with status_server(core) as (url, _, _):
        result = observe.observe(observe.http_collector(core.settings, url), output, samples=1)
    value = records(output)[1]
    assert result["failures"] == 0
    assert value["worker_loop_activity"]["reasons"] == ["LOOP_BASELINE_REQUIRED"]
    assert value["snapshot"]["worker_loop"]["state"] == "RECORDED"
    assert core.settings.database.get_secret_value() not in output.read_text("utf-8")
    assert core.settings.bridge_token.get_secret_value() not in output.read_text("utf-8")
