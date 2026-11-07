"""Durable single-admission N1/N2/N3 over the original native DB and source contracts."""

import asyncio
import json
from contextlib import closing
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet

from companion import budget, erasure
from companion.backup import backup, restore
from companion.contracts import Candidate, CoreError
from companion.core import Core
from companion.diary import day_bounds
from companion.memory_context import MemoryContext
from companion.memory_work import checkpoint_state
from companion.model import ModelResponse
from companion.postgres import Database
from companion.store import Store, encode
from companion.worker_schedule import UnitResult
from tests.conftest import ingest, run
from tests.postgres_fixture import isolated_database, logical_state
from tests.test_consolidation import ReflectionModel, enable_nightly
from tests.test_diary import DAY


def staged(core):
    model = enable_nightly(core)
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 3
    core.settings.extraction_idle_seconds = 0
    model.extraction = encode({"memories": [
        {"subject": "user", "kind": "preference", "fact_key": "reading-plan",
         "content": "我喜欢先列出章节再开始阅读。", "evidence_event_id": "evt_test_3",
         "evidence_quote": "我喜欢先列出章节再开始阅读。"},
        {"subject": "project", "kind": "fact", "fact_key": "reading-week",
         "content": "这周想先读科幻小说。", "evidence_event_id": "evt_test_2",
         "evidence_quote": "这周想先读科幻小说。"},
    ]})
    core.consolidation.schedule_day(DAY)
    return model


def step(core):
    before = core.store.one("SELECT COUNT(*) AS n FROM model_usage")["n"]
    result = run(core.consolidation.run_step("nightly-test"))
    assert isinstance(result, UnitResult)
    assert core.store.one("SELECT COUNT(*) AS n FROM model_usage")["n"] - before <= 1
    return result


def advance_to(core, phase):
    for _ in range(30):
        assert step(core).progressed
        state = json.loads(core.consolidation.status(DAY)["steps_json"])
        if state.get("memory_batch", {}).get("phase") == phase:
            return state
    raise AssertionError("phase not reached: " + phase)


def drain(core):
    for _ in range(50):
        result = step(core)
        if not result.progressed:
            return
    raise AssertionError("nightly step did not quiesce")


def test_n2_builds_real_warm_context_before_separate_extraction_and_review(core):
    model = staged(core)
    assert step(core).progressed  # N1 source/rights collection, zero model admissions.
    assert not model.calls
    assert step(core).progressed  # N2 native episode, does not consume extraction input.
    steps = json.loads(core.consolidation.status(DAY)["steps_json"])
    assert steps["n2"] == "MATERIAL_READY" and len(steps["warm_episode_ids"]) == 1
    assert not core.store.one("SELECT 1 FROM events WHERE memory_processed=1")
    conversation = core.store.one("SELECT conversation_id FROM events LIMIT 1")["conversation_id"]
    context = MemoryContext(core.memory).continuation(conversation, core.clock())
    assert context["items"][0]["episode_id"] == steps["warm_episode_ids"][0]
    assert not model.calls
    assert step(core).progressed
    saved = json.loads(core.consolidation.status(DAY)["steps_json"])
    assert saved["memory_batch"]["phase"] == "REVIEW"
    assert saved["n2"] == "COMPLETE" and saved["n3"] == "RUNNING"
    assert [kind for _, kind in model.calls] == ["memory_extract"]
    assert core.store.rows("SELECT * FROM memories") == []
    assert step(core).progressed
    assert [kind for _, kind in model.calls] == ["memory_extract", "memory_review"]
    assert len(core.store.rows("SELECT * FROM memories")) == 2
    drain(core)
    result = core.consolidation.status(DAY)
    assert result["state"] == "COMPLETE"
    saved = json.loads(result["steps_json"])
    assert all(saved[name] == "COMPLETE" for name in ("n1", "n2", "n3"))
    assert len(saved["warm_memory_ids"]) == 2
    assert "candidates" not in saved["memory_batch"] and "proof" not in saved["memory_batch"]
    assert [kind for _, kind in model.calls].count("diary") == 1
    assert [kind for _, kind in model.calls].count("reflection") == 1


def test_restart_between_extraction_and_review_does_not_extract_twice(core):
    model = staged(core)
    advance_to(core, "REVIEW")
    with Core(core.settings, model, core.clock) as restarted:
        drain(restarted)
    assert core.consolidation.status(DAY)["state"] == "COMPLETE"
    assert [kind for _, kind in model.calls].count("memory_extract") == 1
    assert [kind for _, kind in model.calls].count("memory_review") == 1


@pytest.mark.parametrize("before_reservation", [True, False])
def test_interrupted_intent_distinguishes_no_admission_from_uncheckpointed_result(
    core, monkeypatch, before_reservation,
):
    model = staged(core)
    assert step(core).progressed and step(core).progressed
    if before_reservation:
        original = budget.reserve

        def crash(*args, **kwargs):
            raise SystemExit("before reserve")

        monkeypatch.setattr(budget, "reserve", crash)
    else:
        original = core.consolidation.checkpoint

        def crash(current, *args, **kwargs):
            if current["steps"].get("memory_batch", {}).get("phase") == "REVIEW":
                raise SystemExit("after settled response, before checkpoint")
            return original(current, *args, **kwargs)

        monkeypatch.setattr(core.consolidation, "checkpoint", crash)
    with pytest.raises(SystemExit):
        step(core)
    if before_reservation:
        monkeypatch.setattr(budget, "reserve", original)
    core.clock.advance(core.settings.lease_seconds + 1)
    with Core(core.settings, model, core.clock) as restarted:
        drain(restarted)
    expected = 1 if before_reservation else 2
    assert [kind for _, kind in model.calls].count("memory_extract") == expected
    assert core.consolidation.status(DAY)["state"] == "COMPLETE"


@pytest.mark.parametrize("scenario", ["memory_extract", "memory_review", "diary", "reflection"])
def test_missing_usage_never_commits_or_replays_the_affected_step(core, scenario):
    model = staged(core)
    original = model.complete

    async def missing(messages, kind):
        response = await original(messages, kind)
        return ModelResponse(response.text) if kind == scenario else response

    model.complete = missing
    drain(core)
    unknown = core.store.rows("SELECT * FROM model_usage WHERE state='UNKNOWN'")
    assert len(unknown) == 1 and unknown[0]["scenario"] == scenario
    assert core.consolidation.status(DAY)["state"] == "PARTIAL"
    before = list(model.calls)
    drain(core)
    assert before == model.calls
    assert unknown == core.store.rows("SELECT * FROM model_usage WHERE state='UNKNOWN'")
    if scenario.startswith("memory"):
        assert core.store.rows("SELECT * FROM memories") == []
    elif scenario == "diary":
        assert core.store.rows("SELECT * FROM diary_entries WHERE status='COMMITTED'") == []
    else:
        assert core.store.rows("SELECT * FROM reflection_suggestions") == []


def test_withdraw_pending_candidates_and_derived_episode_in_same_native_erasure(core):
    staged(core)
    advance_to(core, "REVIEW")
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, ["evt_test_3"], core.clock())
    assert core.consolidation.status(DAY)["steps_json"] == "{}"
    assert core.consolidation.status(DAY)["state"] == "CANCELLED"
    assert core.store.rows("SELECT * FROM memories") == []
    assert not core.store.one("SELECT 1 FROM episodes WHERE status='ACTIVE'")
    assert not step(core).progressed


def test_two_workers_cannot_share_an_admitted_step(core):
    model = staged(core)
    assert step(core).progressed and step(core).progressed

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        original = model.complete

        async def wait(messages, kind):
            entered.set()
            await release.wait()
            return await original(messages, kind)

        model.complete = wait
        task = asyncio.create_task(core.consolidation.run_step("one"))
        await asyncio.wait_for(entered.wait(), 3)
        with Core(core.settings, model, core.clock) as second:
            assert not (await second.consolidation.run_step("two")).progressed
        release.set()
        assert (await task).progressed
    run(scenario())
    assert [kind for _, kind in model.calls] == ["memory_extract"]


def test_diary_step_only_consumes_its_owned_day(core):
    staged(core)
    earlier = DAY - timedelta(days=1)
    for number in range(10, 13):
        ingest(core, number, "更早一天的阅读记录", at=earlier.isoformat() + "T04:00:00+00:00")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    earlier_run = core.diary.schedule_day(earlier)
    drain(core)
    earlier_state = core.store.one('SELECT state FROM diary_runs WHERE run_id=%s', (earlier_run,))
    assert earlier_state["state"] == "PENDING"
    assert core.store.one('SELECT state FROM diary_runs WHERE local_date=%s',
                          (DAY.isoformat(),))["state"] == "COMMITTED"


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)])
def test_closed_natural_day_stages_keep_dst_identity(core, day, hours):
    core.settings.timezone = "America/New_York"
    zone = ZoneInfo(core.settings.timezone)
    core.clock.now = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone)
    core.clock.advance(12 * 3600)
    core.settings.consolidation_enabled = core.settings.diary_enabled = True
    core.settings.memory_enabled = core.settings.reflection_enabled = False
    model = ReflectionModel()
    core.model = core.diary.model = core.reflections.model = model
    for number in range(1, 4):
        at = datetime.combine(day, datetime.min.time(), zone) + timedelta(hours=12)
        ingest(core, number, "这一天的真实读书记录", at=at.isoformat())
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.consolidation.schedule_day(day)
    drain(core)
    start, end = (datetime.fromisoformat(value)
                  for value in day_bounds(day, core.settings.timezone))
    assert (end - start).total_seconds() == hours * 3600
    assert core.consolidation.status(day)["state"] == "COMPLETE"
    assert core.store.one("SELECT COUNT(*) AS n FROM consolidation_runs")["n"] == 1


def test_empty_closed_day_has_no_model_admission(core):
    model = enable_nightly(core, seed=False)
    core.settings.memory_enabled = True
    core.consolidation.schedule_day(DAY)
    assert step(core).progressed
    assert core.consolidation.status(DAY)["state"] == "SKIPPED"
    assert model.calls == [] and core.store.rows("SELECT * FROM model_usage") == []


def test_n2_reuses_prior_extraction_episodes_instead_of_overlapping_sources(core):
    model = enable_nightly(core, seed=False)
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 2
    core.settings.extraction_idle_seconds = 0
    for group in ((1, 2), (3, 4)):
        for number in group:
            ingest(core, number, f"第{number}条真实项目记录",
                   at=DAY.isoformat() + "T04:00:00+00:00")
        core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
        assert run(core.extract_once(DAY))
    before = core.store.rows("SELECT * FROM episodes ORDER BY episode_id")
    assert len(before) == 2
    core.consolidation.schedule_day(DAY)
    assert step(core).progressed and step(core).progressed
    assert core.store.rows("SELECT * FROM episodes ORDER BY episode_id") == before
    assert all(row["n"] == 1 for row in core.store.rows(
        "SELECT event_id,COUNT(*) AS n FROM episode_events GROUP BY event_id",
    ))
    assert [kind for _, kind in model.calls].count("memory_extract") == 2


def test_standalone_step_and_nightly_share_one_checkpoint(core):
    model = staged(core)
    assert run(core.extract_step(DAY)).progressed
    original = checkpoint_state(core.store)
    assert original["batch"]["phase"] == "REVIEW"
    assert len(model.calls) == 1
    drain(core)
    assert core.consolidation.status(DAY)["state"] == "COMPLETE"
    assert checkpoint_state(core.store)["batch_id"] == original["batch_id"]
    assert [kind for _, kind in model.calls].count("memory_extract") == 1
    assert [kind for _, kind in model.calls].count("memory_review") == 1


def test_standalone_steps_resume_with_no_nightly_run_and_quiesce(core):
    model = staged(core)
    core.store.execute("DELETE FROM consolidation_runs")
    assert run(core.extract_step()).progressed
    assert [kind for _, kind in model.calls] == ["memory_extract"]
    with Core(core.settings, model, core.clock) as restarted:
        assert run(restarted.extract_step()).progressed
        before = logical_state(restarted.store)
        assert not run(restarted.extract_step()).progressed
        assert logical_state(restarted.store) == before
    assert [kind for _, kind in model.calls] == ["memory_extract", "memory_review"]
    assert len(core.store.rows("SELECT * FROM memories")) == 2
    assert core.store.rows("SELECT * FROM consolidation_runs") == []


@pytest.mark.parametrize("during_call", [True, False])
def test_standalone_checkpoint_erasure_and_restore_cannot_revive_candidates(
    core, tmp_path, during_call,
):
    model = staged(core)
    core.store.execute("DELETE FROM consolidation_runs")
    if during_call:
        def withdraw():
            with core.store.transaction(control_write=True):
                core.ledger.expire_events(core.store, ["evt_test_3"], core.clock())
        model.callback = withdraw
        assert not run(core.extract_step()).progressed
        assert checkpoint_state(core.store) is None
    else:
        assert run(core.extract_step()).progressed
        key = Fernet.generate_key()
        archive = tmp_path / "pending.enc"
        backup(core.settings.database, archive, key)
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, ["evt_test_3"], core.clock())
        assert checkpoint_state(core.store) is None
        with isolated_database() as restored, Database(restored) as database:
            state = tmp_path / "restored-state"
            restore(archive, restored, key, core.settings.erasure_ledger, state_directory=state)
            with closing(Store(database, state)) as reopened:
                assert checkpoint_state(reopened) is None
    assert core.store.rows("SELECT * FROM memories") == []


def test_n2_encodes_only_new_sources_and_second_nightly_is_idempotent(core):
    enable_nightly(core, seed=False)
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 2
    core.settings.extraction_idle_seconds = 0
    for number in (1, 2):
        ingest(core, number, "已整理的项目记录" + str(number),
               at=DAY.isoformat() + "T04:00:00+00:00")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    assert run(core.extract_once(DAY))
    original = core.store.one("SELECT * FROM episodes")
    ingest(core, 3, "后来新增的真实记录", at=DAY.isoformat() + "T04:00:00+00:00")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.consolidation.schedule_day(DAY)
    drain(core)
    episodes = core.store.rows("SELECT * FROM episodes ORDER BY episode_id")
    assert len(episodes) == 2 and original in episodes
    assert core.store.one("SELECT memory_processed FROM events WHERE event_id='evt_test_3'") == {
        "memory_processed": 0}  # Native minimum batch threshold still applies.
    core.consolidation.schedule_day(DAY)
    drain(core)
    assert core.store.rows("SELECT * FROM episodes ORDER BY episode_id") == episodes
    assert all(row["n"] == 1 for row in core.store.rows(
        "SELECT event_id,COUNT(*) AS n FROM episode_events GROUP BY event_id",
    ))


def test_later_extraction_reuses_n2_episode_and_only_encodes_new_source(core):
    enable_nightly(core, seed=False)
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 2
    core.settings.extraction_idle_seconds = 0
    for number in (1, 2):
        ingest(core, number, "已接入的项目记录" + str(number),
               at=DAY.isoformat() + "T04:00:00+00:00")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.consolidation.schedule_day(DAY)
    assert step(core).progressed and step(core).progressed
    original = core.store.one("SELECT * FROM episodes")
    assert not core.store.one("SELECT 1 FROM events WHERE memory_processed=1")
    ingest(core, 3, "提取前新增的记录", at=DAY.isoformat() + "T04:00:00+00:00")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    assert run(core.extract_once(DAY))
    episodes = core.store.rows("SELECT * FROM episodes")
    assert len(episodes) == 2 and original in episodes
    assert all(row["n"] == 1 for row in core.store.rows(
        "SELECT event_id,COUNT(*) AS n FROM episode_events GROUP BY event_id",
    ))
    assert not core.store.one("SELECT 1 FROM events WHERE memory_processed=0")


def test_standalone_completion_is_observed_by_waiting_nightly(core):
    model = staged(core)
    advance_to(core, "REVIEW")
    assert run(core.extract_step()).progressed
    assert checkpoint_state(core.store)["batch"]["phase"] == "COMPLETE"
    drain(core)
    assert core.consolidation.status(DAY)["state"] == "COMPLETE"
    assert [kind for _, kind in model.calls].count("memory_extract") == 1
    assert [kind for _, kind in model.calls].count("memory_review") == 1


def test_timezone_change_retires_pending_work_without_crossing_day_scope(core):
    model = staged(core)
    assert run(core.extract_step(DAY)).progressed
    usage = core.store.rows("SELECT * FROM model_usage")
    core.settings.timezone = "UTC"
    assert run(core.extract_step()).progressed
    record = checkpoint_state(core.store)
    assert record["batch"]["phase"] == "CANCELLED"
    assert "candidates" not in record["batch"] and "proof" not in record["batch"]
    assert usage == core.store.rows("SELECT * FROM model_usage")
    assert len(model.calls) == 1
    assert run(core.extract_step(DAY)).progressed
    assert checkpoint_state(core.store)["timezone"] == "UTC"


def test_standalone_pending_sources_expire_without_waiting_for_cleanup(core):
    model = staged(core)
    assert run(core.extract_step()).progressed
    usage = core.store.rows("SELECT * FROM model_usage")
    core.clock.advance((core.settings.raw_event_retention_days + 1) * 86400)
    assert not run(core.extract_step()).progressed
    assert checkpoint_state(core.store) is None
    assert usage == core.store.rows("SELECT * FROM model_usage")
    assert len(model.calls) == 1 and core.store.rows("SELECT * FROM memories") == []


def test_pending_work_requires_the_native_erasure_registration(core, monkeypatch):
    model = staged(core)
    contracts = erasure.cleanup_contracts()
    monkeypatch.setattr(erasure, "cleanup_contracts", lambda: tuple(
        contract for contract in contracts if contract.name != "memory-work"
    ))
    with pytest.raises(CoreError, match="MEMORY_ERASURE_CONTRACT_REQUIRED"):
        run(core.extract_step())
    assert checkpoint_state(core.store) is None and model.calls == []


def test_native_no_review_shortcut_does_not_claim_an_unmade_call(core):
    model = staged(core)
    candidates = json.loads(model.extraction)["memories"]
    model.extraction = encode({"memories": candidates[:1]})
    # The no-call path is reuse of an already supported identical fact. A lone
    # new inference must now be reviewed even without an existing catalog.
    core.memory.add(Candidate.model_validate(candidates[0]), core.clock())
    assert run(core.extract_step()).progressed
    assert run(core.extract_step()).progressed
    record = checkpoint_state(core.store)
    assert record["batch"]["phase"] == "COMPLETE"
    assert "request_id" not in record["batch"]
    assert core.store.one('SELECT state FROM model_usage WHERE request_id=%s',
                          (record["batch"]["extraction_request_id"],)) == {"state": "SETTLED"}
    assert [kind for _, kind in model.calls] == ["memory_extract"]
