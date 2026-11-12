"""Finite turns, native silence and foreground precedence without another queue."""

import asyncio
from datetime import datetime, timedelta
from time import monotonic

import pytest

from companion import __main__, cloud_budget, life_worker, worker_deadlines
from companion.autonomous_selector import Lane, RoundRobin, native_selector
from companion.contracts import CoreError
from companion.worker_schedule import UnitResult
from tests.conftest import ingest, run
from tests.support.autonomous_selector import drain
from tests.support.consolidation import enable_nightly
from tests.support.diary import DAY, seed_day
from tests.support.goals import setup_goal
from tests.support.life_topics import bug, proposal
from tests.support.social_autonomy import background


def test_fenced_units_consume_turns_and_idle_sweep_is_finite():
    calls = []

    def lane(name):
        async def execute():
            calls.append(name)
            return UnitResult(False)

        return Lane(name, execute)

    selector = RoundRobin(tuple(lane(name) for name in ("life", "diary", "social")), lambda: True)

    async def scenario():
        for expected in ("life", "diary", "social"):
            before = len(calls)
            selected = await selector.step()
            assert selected.lane == expected and len(calls) == before + 1
        assert not (await selector.step()).more
        assert calls == ["life", "diary", "social"]
        selector.resume()
        await drain(selector)
        assert calls == ["life", "diary", "social"] * 2

    run(scenario())


def test_progress_rotates_and_foreground_is_rechecked_before_every_turn():
    calls, waiting = [], [False]

    def lane(name):
        async def execute():
            calls.append(name)
            return UnitResult(True)

        return Lane(name, execute)

    selector = RoundRobin(tuple(lane(name) for name in ("life", "diary", "social")),
                          lambda: not waiting[0])

    async def scenario():
        for _ in range(3):
            await selector.step()
        waiting[0] = True
        selected = await selector.step()
        assert selected.blocked and not selected.more
        assert calls == ["life", "diary", "social"]
        waiting[0] = False
        selector.resume()
        for _ in range(3):
            await selector.step()
        assert calls == ["life", "diary", "social"] * 2

    run(scenario())


def test_native_off_sweep_does_not_create_model_requests_or_activity(core):
    run(drain(native_selector(core, "life-test")))
    assert not core.store.rows("SELECT * FROM model_usage")
    assert not core.store.rows("SELECT * FROM jobs")
    assert not core.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("feature,owner,method,code", [
    ("memory_enabled", "core", "extract_step", "NATIVE_MEMORY_STEP_REQUIRED"),
    ("consolidation_enabled", "consolidation", "run_step", "NATIVE_CONSOLIDATION_STEP_REQUIRED"),
])
def test_missing_single_step_interface_cannot_fall_back_to_multi_model_operations(
    core, monkeypatch, feature, owner, method, code,
):
    setattr(core.settings, feature, True)
    target = core if owner == "core" else core.consolidation
    monkeypatch.setattr(target, method, None, raising=False)
    with pytest.raises(CoreError, match=code):
        native_selector(core, "life-test")
    assert not core.model.calls


def test_native_shadow_skip_consumes_topic_without_sending_or_repaying(core):
    bug(core)
    proposal(core, action="SKIP", destination="NONE", text="", reason="无需继续")
    core.clock.advance(86400)
    selector = native_selector(core, "life-test")

    async def scenario():
        for _ in range(2):
            selector.resume()
            for _ in range(100):
                before = core.store.one("SELECT COUNT(*) n FROM model_usage")["n"]
                selected = await selector.step()
                after = core.store.one("SELECT COUNT(*) n FROM model_usage")["n"]
                assert after - before <= 1
                if not selected.more:
                    break
            else:
                pytest.fail("SKIP did not become idle")

    run(scenario())
    assert len(core.model.calls) == 1
    assert core.store.one("SELECT state FROM life_activity_results")["state"] == "SKIPPED"
    assert core.store.one("SELECT state FROM life_topics")["state"] == "CLOSED"
    assert not core.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("fence", ["quiet", "daily", "pause"])
def test_native_life_fences_remain_silent_when_selector_revisits(core, fence):
    bug(core)
    proposal(core)
    core.clock.advance(86400)
    if fence == "quiet":
        core.settings.quiet_start, core.settings.quiet_end = 0, 23
    elif fence == "daily":
        core.settings.life_loop = core.settings.life_loop.model_copy(
            update={"max_daily_decisions": 0},
        )
    else:
        core.goals.pause_autonomy(True)
    selector = native_selector(core, "life-test")
    for _ in range(2):
        selector.resume()
        run(drain(selector))
    assert not core.model.calls
    assert not core.store.rows("SELECT * FROM life_activity_results")
    assert not core.store.rows("SELECT * FROM outbox")


def test_native_defer_waits_for_cooldown_then_requires_new_evidence(core, monkeypatch):
    from datetime import datetime

    from companion import autonomous_selector

    # Pacing is monotonic; in production it advances together with the wall clock.
    monkeypatch.setattr(autonomous_selector, "monotonic",
                        lambda: datetime.fromisoformat(core.clock()).timestamp())
    bug(core)
    proposal(core, "DEFER", "NONE", text="", defer_seconds=60)
    core.clock.advance(86400)
    selector = native_selector(core, "life-test")
    run(drain(selector))
    assert len(core.model.calls) == 1
    core.clock.advance(61)
    selector.resume()
    run(drain(selector))
    assert len(core.model.calls) == 1
    core.clock.advance(3600)
    selector.resume()
    run(drain(selector))
    assert len(core.model.calls) == 2
    assert core.store.one("SELECT state,reason_code FROM life_topics") == {
        "state": "WAITING", "reason_code": "WAITING_FOR_NEW_EVIDENCE"}
    core.clock.advance(3601)
    selector.resume()
    run(drain(selector))
    assert len(core.model.calls) == 2
    assert not core.store.rows("SELECT * FROM outbox")


def test_new_owner_input_during_life_model_preempts_following_turns(core):
    bug(core)
    proposal(core)
    core.clock.advance(86400)
    core.model.callback = lambda: ingest(core, 2, "先回答我这个新的问题")
    selector = native_selector(core, "life-test")
    turns = run(drain(selector))
    assert turns[-1].blocked
    assert len(core.model.calls) == 1
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    assert not core.store.rows("SELECT * FROM life_activity_results WHERE text<>''")
    assert not core.store.rows("SELECT * FROM outbox")


def test_diary_is_selected_and_committed_through_original_native_service(core):
    model = seed_day(core)
    run(drain(native_selector(core, "life-test")))
    assert len(model.calls) == 1
    assert core.store.one("SELECT state FROM diary_runs WHERE local_date='2026-10-18'")[
        "state"] == "COMMITTED"
    assert not core.store.rows("SELECT * FROM outbox")


def test_native_social_skip_does_not_create_a_second_scheduler_job_or_repay(core):
    module, _, _ = background(core)
    module.model.proposal = {"kind": "skip", "reason_code": "NO_ACTION"}
    core.model = module.model
    selector = native_selector(core, "life-test")
    run(drain(selector))
    selector.resume()
    run(drain(selector))
    assert len(module.model.calls) == 1
    assert core.store.one("SELECT state FROM social_autonomy_plans")["state"] == "SKIPPED"
    assert not core.store.rows("SELECT * FROM outbox")
    assert not core.store.rows("SELECT * FROM jobs WHERE kind='social_plan'")


def test_social_budget_deferral_reuses_original_native_job_after_deadline(core, monkeypatch):
    module, _, _ = background(core)
    module.model.proposal = {"kind": "skip", "reason_code": "NO_ACTION"}
    core.model = module.model
    selector = native_selector(core, "life-test")

    def refuse(*_args, **_kwargs):
        raise CoreError("CLOUD_DAILY_BUDGET_EXHAUSTED", 429)

    with monkeypatch.context() as patch:
        patch.setattr(cloud_budget, "reserve", refuse)
        run(drain(selector))
    job = core.store.one("SELECT * FROM jobs WHERE kind='social_plan'")
    assert job["state"] == "QUEUED" and not module.model.calls
    assert not core.store.rows("SELECT * FROM model_usage")
    hint = worker_deadlines.life(core.store, core.settings, datetime.fromisoformat(core.clock()))
    assert abs((hint - datetime.fromisoformat(job["next_attempt_at"])).total_seconds()) < 0.01
    core.clock.advance(61)
    selector.resume()
    run(drain(selector))
    assert len(module.model.calls) == 1
    assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (job["job_id"],))[
        "state"] == "SUCCEEDED"
    assert core.store.one("SELECT COUNT(*) n FROM jobs WHERE kind='social_plan'")["n"] == 1
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    assert not core.store.rows("SELECT * FROM outbox")


def test_life_topic_due_boundary_is_included_after_selector_migration(core):
    topic = bug(core)
    due = datetime.fromisoformat(topic["due_at"])
    core.clock.now = due - timedelta(seconds=1)
    hint = worker_deadlines.life(core.store, core.settings, core.clock.now)
    assert abs((hint - due).total_seconds()) < 0.01


def test_retired_diary_command_cannot_start_another_selector(monkeypatch):
    monkeypatch.setattr("sys.argv", ["companion", "diary-worker"])
    with pytest.raises(SystemExit) as error:
        __main__.main()
    assert error.value.code == 2


def test_owner_pending_blocks_all_autonomous_lanes_but_not_native_chat(core):
    ingest(core)
    selector = native_selector(core, "life-test")
    maintenance = {lane.name for lane in selector.lanes if lane.maintenance}
    assert maintenance == {"retention", "media_cleanup", "diary_schedule",
                           "consolidation_schedule", "commitment_expiry"}
    turns = [run(selector.step()) for _ in range(len(selector.lanes) + 1)]
    assert all(turn.blocked for turn in turns)
    # Only local bookkeeping ran while the owner was waiting; no autonomous model lane.
    assert {turn.lane for turn in turns} - {None} == maintenance and turns[-1].lane is None
    assert not core.model.calls
    assert run(core.run_once("foreground"))
    assert core.store.one("SELECT state FROM jobs WHERE kind='chat'")["state"] == "SUCCEEDED"


def test_chat_bound_content_dependency_keeps_running_while_selector_yields(core, monkeypatch):
    from companion import content
    from tests.support.article_vision import article
    from tests.support.content import enable, png

    service, provider = enable(core)
    owner_id, article_id = article(core, 1)
    job = core.store.one("SELECT * FROM jobs WHERE kind='chat'")
    assert core.claim_job("foreground") is None
    assert run(native_selector(core, "life-test").step()).blocked

    async def fetch(*_args, **_kwargs):
        return png(), "image/png"

    monkeypatch.setattr(content, "fetch_public_image", fetch)
    assert run(service.run_batch()) == 1
    image = core.store.one("SELECT * FROM content_items WHERE kind='link_image'")
    assert (image["owner_event_id"], image["source_event_id"]) == (owner_id, article_id)
    assert image["state"] == "SUCCEEDED" and len(provider.calls) == 1
    assert run(core.run_once("foreground"))
    finished = core.store.one('SELECT * FROM jobs WHERE job_id=%s', (job["job_id"],))
    assert finished["event_id"] == owner_id and finished["state"] == "SUCCEEDED"


def test_blocked_life_worker_does_not_spin_on_overdue_maintenance(core, monkeypatch):
    ingest(core)
    monkeypatch.setattr(life_worker, "Core", lambda *_: core)
    waited = []

    async def stop(self, scan, result, maintenance_at):
        # Due bookkeeping already ran under foreground precedence; the blocked
        # wait targets the next future maintenance, never an overdue instant.
        waited.append(maintenance_at)
        assert maintenance_at is not None and maintenance_at > monotonic()
        raise asyncio.CancelledError

    monkeypatch.setattr(life_worker.Wakeup, "wait", stop)
    with pytest.raises(asyncio.CancelledError):
        run(life_worker.life_worker(core.settings))
    assert len(waited) == 1


def test_failing_lane_is_isolated_backs_off_and_later_lanes_still_run(monkeypatch):
    from companion import autonomous_selector

    clock, calls = [1000.0], []
    monkeypatch.setattr(autonomous_selector, "monotonic", lambda: clock[0])

    async def broken():
        calls.append("social")
        raise RuntimeError("synthetic domain failure")

    async def diary():
        calls.append("diary")
        return UnitResult(False)

    selector = RoundRobin((Lane("social", broken), Lane("diary", diary)), lambda: True)

    async def scenario():
        failed = await selector.step()
        assert failed.lane == "social" and not failed.result.progressed and failed.more
        assert (await selector.step()).lane == "diary"
        selector.resume()
        await drain(selector)
        assert calls == ["social", "diary", "diary"]  # still backing off
        clock[0] += autonomous_selector.FAILURE_BACKOFF_SECONDS
        selector.resume()
        await drain(selector)
        assert calls[-2:] == ["social", "diary"]
        assert selector.lanes[0].failures == 2
        backoff = autonomous_selector.FAILURE_BACKOFF_SECONDS
        assert selector.lanes[0].retry_at == clock[0] + 2 * backoff

    run(scenario())


def test_failing_maintenance_lane_does_not_report_an_overdue_wake(monkeypatch):
    from companion import autonomous_selector

    clock = [1000.0]
    monkeypatch.setattr(autonomous_selector, "monotonic", lambda: clock[0])

    async def broken():
        raise RuntimeError("synthetic maintenance failure")

    selector = RoundRobin((Lane("retention", broken, interval=60),), lambda: True)
    run(selector.step())
    assert selector.next_maintenance == clock[0] + autonomous_selector.FAILURE_BACKOFF_SECONDS


def test_selector_failure_does_not_swallow_cancellation():
    async def cancelled():
        raise asyncio.CancelledError

    selector = RoundRobin((Lane("diary", cancelled),), lambda: True)
    with pytest.raises(asyncio.CancelledError):
        run(selector.step())


def test_life_worker_refuses_to_start_beside_an_orphaned_diary_worker(core, monkeypatch, capsys):
    from companion.worker_lock import single_worker

    started = []

    async def never(settings):
        started.append(settings)

    monkeypatch.setattr("companion.life_worker.life_worker", never)
    monkeypatch.setattr(__main__.Settings, "load", staticmethod(lambda: core.settings))
    monkeypatch.setattr("sys.argv", ["companion", "life-worker"])
    with single_worker(core.settings.state_directory, "diary-worker"):
        with pytest.raises(SystemExit) as error:
            __main__.main()
    assert error.value.code == 2 and started == []
    assert "WORKER_ALREADY_RUNNING" in capsys.readouterr().err
    __main__.main()  # the retired lock is free again, so the single Life worker starts
    assert started == [core.settings]


def test_foreground_precedence_pauses_model_lanes_but_not_local_bookkeeping():
    calls = []

    def lane(name, *, maintenance=False):
        async def execute():
            calls.append(name)
            return UnitResult(maintenance)

        return Lane(name, execute, interval=60 if maintenance else None, maintenance=maintenance)

    selector = RoundRobin((lane("retention", maintenance=True), lane("diary"),
                           lane("diary_schedule", maintenance=True)), lambda: False)

    async def scenario():
        turns = [await selector.step() for _ in range(4)]
        assert [turn.lane for turn in turns] == ["retention", "diary_schedule", None, None]
        assert all(turn.blocked for turn in turns)
        # Only still-due bookkeeping continues the blocked turn without a wait.
        assert [turn.more for turn in turns] == [True, False, False, False]
        assert calls == ["retention", "diary_schedule"]  # the model lane never ran
        assert selector.next_blocked_maintenance > monotonic()

    run(scenario())


def test_maintenance_lanes_report_only_committed_work(core):
    from companion import image_generation

    enable_nightly(core)
    core.settings.diary_catchup_days = core.settings.consolidation_catchup_days = 1
    goal = setup_goal(core, number=50, commitment=True)
    lanes = {lane.name: lane for lane in native_selector(core, "maintenance").lanes}

    def progressed(name):
        return run(lanes[name].run()).progressed

    for name, schedule in (("diary_schedule", core.diary.schedule_day),
                           ("consolidation_schedule", core.consolidation.schedule_day)):
        assert progressed(name)
        run_id = schedule(DAY)
        assert run_id and schedule(DAY) == run_id
        assert not progressed(name)
    assert not progressed("commitment_expiry")
    core.clock.advance(86401)
    assert progressed("commitment_expiry")
    assert core.goals.get(goal)["state"] == "FAILED"
    assert not progressed("commitment_expiry")

    root = image_generation.directory(core.store)
    root.mkdir(mode=0o700, exist_ok=True)
    assert not progressed("media_cleanup")
    (root / "orphan.tmp").write_bytes(b"orphan")
    assert progressed("media_cleanup")
    assert not progressed("media_cleanup")


def test_idle_fast_lane_waits_its_idle_interval_and_keeps_pace_when_busy(monkeypatch):
    from companion import autonomous_selector

    clock, outcomes = [1000.0], [False, True]
    monkeypatch.setattr(autonomous_selector, "monotonic", lambda: clock[0])

    async def index():
        return UnitResult(outcomes.pop(0))

    selector = RoundRobin(
        (Lane("memory_index", index, interval=1.0, idle_interval=60.0),), lambda: True)
    run(selector.step())
    # Nothing to warm: the next wake is a minute away, not one second.
    assert selector.next_maintenance == clock[0] + 60.0
    clock[0] += 60.0
    selector.resume()
    run(selector.step())
    # Real progress keeps the fast cadence.
    assert selector.next_maintenance == clock[0] + 1.0


def test_native_life_topic_lane_is_paced_instead_of_running_every_wake(core):
    from companion.worker_wakeup import MAINTENANCE_SECONDS

    lanes = {lane.name: lane for lane in native_selector(core, "life-test").lanes}
    topic = lanes["life_topic"]
    assert (topic.interval, topic.idle_interval) == (0.0, MAINTENANCE_SECONDS)
    assert not topic.maintenance


def test_scheduling_hint_reuses_one_authority_view_per_window(core, monkeypatch):
    from companion import autonomous_selector, owner_authority

    clock, calls = [1000.0], []
    monkeypatch.setattr(autonomous_selector, "monotonic", lambda: clock[0])
    monkeypatch.setattr(owner_authority, "current",
                        lambda *a, **k: calls.append(1) or "view")
    hint = autonomous_selector.HintView(core, seconds=5.0)
    assert [hint.get() for _ in range(20)] == ["view"] * 20
    assert len(calls) == 1
    clock[0] += 5.0
    assert hint.get() == "view" and len(calls) == 2

    def refused(*args, **kwargs):
        raise CoreError("OWNER_AUTHORITY_DISABLED", 403)

    monkeypatch.setattr(owner_authority, "current", refused)
    clock[0] += 5.0
    assert hint.get() is None
