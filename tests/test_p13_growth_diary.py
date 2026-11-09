"""Synthetic regressions only; clock advances do not establish real natural days."""

import pytest
from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.diary import DiaryDraft, render_draft
from companion.goals import GoalProposal, GoalScope
from companion.source_grants import ReaderReadValidation
from tests.conftest import ingest, run
from tests.support.consolidation import enable_nightly
from tests.support.diary import DAY
from tests.support.growth_foundations import complete_read, view
from tests.support.heybox_reader import URL
from tests.support.observation_capture import enable_observations, observation
from tests.support.owner_authority import activate as activate_owner
from tests.support.owner_authority_setup import prepare as prepare_owner
from tests.support.source_grants import prepare, receipt


def test_live_daily_sources_exclude_retained_simulation_before_any_model_request(core, tmp_path):
    enable_observations(core)
    core.observations.append(
        "reader", observation(core, occurred_at="2026-10-18T04:00:00Z")
    )
    assert core.diary.repo.sources(DAY)
    core.settings.mode = "live"
    assert core.diary.repo.sources(DAY) == []
    enable_nightly(core, seed=False)
    policy = prepare_owner(core, tmp_path).model_copy(update={"purposes": ["reflection"]})
    activate_owner(core, policy)
    assert run(core.reflections.run_day(DAY))["state"] == "SKIPPED"
    assert core.model.calls == []
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_demo_daily_sources_keep_simulated_coverage_explicit(core):
    enable_observations(core)
    core.observations.append(
        "reader", observation(core, occurred_at="2026-10-18T04:00:00Z")
    )
    source = core.diary.repo.sources(DAY)[0]
    assert source["source"] == "observation.simulation"
    assert source["evidence_level"] == "SIMULATED_FETCHED"


def test_live_daily_source_limit_is_applied_after_excluding_simulations(core):
    enable_observations(core)
    for number in range(1, 11):
        core.observations.append("reader", observation(
            core, number, occurred_at="2026-10-18T04:00:00Z",
        ))
    enable_observations(core, source="heybox")
    core.observations.append("reader", observation(
        core, 11, source="heybox", occurred_at="2026-10-18T05:00:00Z",
        payload={"text": "已获取的文章内容", "url": URL},
    ))
    core.settings.mode = "live"
    sources = core.diary.repo.sources(DAY)
    assert len(sources) == 1 and sources[0]["event_id"] == "obs_test_11"
    assert sources[0]["source"] == "observation.heybox"


@pytest.mark.parametrize("decision", ["REJECT", "NEED_MORE_EVIDENCE"])
def test_withdrawing_reflection_retracts_its_unstarted_goal(core, decision):
    enable_nightly(core)
    core.settings.goals_enabled = True
    core.consolidation.schedule_day(DAY)
    assert run(core.consolidation.run_once())
    assert core.consolidation.status(DAY)["state"] == "COMPLETE"
    suggestion = core.reflections.recent()[0]
    goal = core.goals.recent()[0]
    assert goal["state"] == "CANDIDATE"
    core.reflections.decide(suggestion["suggestion_id"], decision)
    assert core.goals.get(goal["goal_id"])["state"] == "CANCELLED"
    assert core.goals.accept(goal["goal_id"])["state"] == "CANCELLED"
    assert core.store.rows("SELECT * FROM outbox") == []


def test_reflection_command_does_not_present_owner_rejection_as_pending(core):
    enable_nightly(core)
    assert run(core.reflections.run_day(DAY))["state"] == "COMMITTED"
    suggestion = core.reflections.recent()[0]
    core.reflections.decide(suggestion["suggestion_id"], "REJECT")
    assert core.life.command("/反思列表", "evt_test_1") == "目前没有待核对的反思。"


def reflected_proposal(core, scope=None):
    assert run(core.reflections.run_day(DAY))["state"] == "COMMITTED"
    suggestion = core.reflections.recent()[0]
    source_ids = [row["event_id"] for row in core.store.rows(
        'SELECT event_id FROM reflection_evidence WHERE run_id=%s', (suggestion["run_id"],)
    )]
    proposal = GoalProposal(
        topic=suggestion["content"], source_event_ids=source_ids, scope=scope or GoalScope()
    )
    return suggestion, proposal


def attributed_read(core):
    enable_nightly(core)
    snapshot = prepare(core)
    suggestion, proposal = reflected_proposal(core, GoalScope(operation="read_content", url=URL))
    goal = core.goals.propose(proposal, reflection_suggestion_id=suggestion["suggestion_id"])
    core.goals.accept(goal)
    assert core.exploration.schedule_goal()
    request = core.store.one("SELECT * FROM exploration_requests")
    return suggestion, goal, request, snapshot


def test_simulated_observation_is_labelled_in_diary_and_reflection_output(core):
    enable_observations(core)
    core.observations.append(
        "reader", observation(core, occurred_at="2026-10-18T04:00:00Z")
    )
    source = core.diary.repo.sources(DAY)[0]
    draft = DiaryDraft.model_validate({
        "schema_version": 1, "local_date": DAY.isoformat(), "title": "合成来源核查",
        "worth_saving": True, "sections": [{"kind": "observation", "text": source["text"],
            "evidence": [{"source_id": source["source_id"], "exact_quote": source["text"]}]}],
    })
    assert render_draft(draft, [source], DAY.isoformat(), core.settings).startswith("模拟获取")
    model = enable_nightly(core, seed=False)
    core.settings.reflection_min_sources = 1

    def use_observation(draft):
        draft["suggestions"][0].update(kind="observation", text=source["text"])

    model.reflection_transform = use_observation
    assert run(core.reflections.run_day(DAY))["state"] == "COMMITTED"
    assert core.reflections.recent()[0]["content"].startswith("模拟来源记录（FETCHED）")


def test_reflection_veto_does_not_cancel_identical_independent_goal_or_reactivate_on_reaccept(core):
    enable_nightly(core)
    core.settings.goals_enabled = True
    suggestion, proposal = reflected_proposal(core)
    independent = core.goals.propose(proposal)
    derived = core.goals.propose(proposal, reflection_suggestion_id=suggestion["suggestion_id"])
    assert derived != independent and core.goals.propose(proposal) == independent
    assert core.goals.propose(
        proposal, reflection_suggestion_id=suggestion["suggestion_id"]
    ) == derived
    protected = {table: core.store.rows("SELECT * FROM " + table)
                 for table in ("memories", "traits", "consents", "model_usage", "outbox")}
    core.reflections.decide(suggestion["suggestion_id"], "REJECT")
    assert core.goals.get(independent)["state"] == "CANDIDATE"
    assert core.goals.get(derived)["state"] == "CANCELLED"
    core.reflections.decide(suggestion["suggestion_id"], "ACCEPT_AS_HYPOTHESIS")
    assert core.goals.accept(derived)["state"] == "CANCELLED"
    assert {table: core.store.rows("SELECT * FROM " + table) for table in protected} == protected


@pytest.mark.parametrize("change", ["topic", "sources", "kind"])
def test_reflection_origin_does_not_authorize_a_different_proposal(core, change):
    enable_nightly(core)
    core.settings.goals_enabled = True
    suggestion, proposal = reflected_proposal(core)
    if change == "topic":
        proposal = proposal.model_copy(update={"topic": "无关主题"})
    elif change == "sources":
        proposal = proposal.model_copy(update={"source_event_ids": proposal.source_event_ids[:1]})
    with pytest.raises(CoreError, match="GOAL_REFLECTION_MISMATCH"):
        core.goals.propose(proposal, kind="commitment" if change == "kind" else "self",
                           reflection_suggestion_id=suggestion["suggestion_id"])
    assert core.store.rows("SELECT * FROM goals") == []


@pytest.mark.parametrize("started", [False, True])
def test_reflection_withdrawal_stops_reader_without_refunding_started_io(core, started):
    suggestion, goal, request, snapshot = attributed_read(core)
    item = core.exploration.lease("heybox", snapshot) if started else None
    before = core.store.one("SELECT * FROM exploration_requests")
    core.reflections.decide(suggestion["suggestion_id"], "REJECT")
    after = core.store.one("SELECT * FROM exploration_requests")
    assert after["state"] == ("UNKNOWN" if started else "CANCELLED")
    for field in (
        "reserved_bytes", "reserved_requests", "used_bytes", "used_requests", "started_at"
    ):
        assert after[field] == before[field]
    assert core.goals.get(goal)["state"] == "CANCELLED"
    assert core.store.one("SELECT state FROM goal_steps")["state"] == (
        "EXECUTION_UNCERTAIN" if started else "CANCELLED"
    )
    assert core.exploration.lease("heybox", snapshot) is None
    assert core.goals.claim() is None
    if item:
        validation = ReaderReadValidation(lease_token=item["lease_token"], source_grant=snapshot)
        with pytest.raises(CoreError, match="STALE_READER_LEASE"):
            core.exploration.validate_read("heybox", request["request_id"], validation)
        with pytest.raises(CoreError, match="STALE_READER_LEASE"):
            core.exploration.complete("heybox", request["request_id"], receipt(item, snapshot))
    assert core.store.rows("SELECT * FROM observations") == []
    assert core.store.rows("SELECT * FROM outbox") == []


def test_final_reader_guard_rechecks_goal_after_autonomy_pause(core):
    _, _, request, snapshot = attributed_read(core)
    item = core.exploration.lease("heybox", snapshot)
    validation = ReaderReadValidation(lease_token=item["lease_token"], source_grant=snapshot)
    assert core.exploration.validate_read("heybox", request["request_id"], validation) == {
        "valid": True
    }
    before = core.store.one("SELECT * FROM exploration_requests")
    core.goals.pause_autonomy(True)
    with pytest.raises(CoreError, match="GOAL_STALE_LEASE"):
        core.exploration.validate_read("heybox", request["request_id"], validation)
    assert core.store.one("SELECT * FROM exploration_requests") == before


def test_rejecting_hypothesis_does_not_erase_an_actual_completed_result(core):
    suggestion, goal, request, snapshot = attributed_read(core)
    item = core.exploration.lease("heybox", snapshot)
    result = core.exploration.complete("heybox", request["request_id"], receipt(item, snapshot))
    before = core.store.one("SELECT * FROM exploration_requests")
    core.reflections.decide(suggestion["suggestion_id"], "REJECT")
    assert core.goals.get(goal)["state"] == "DONE"
    assert core.goals.get(goal)["result_event_id"] == result["event_ids"][0]
    assert core.store.one("SELECT * FROM exploration_requests") == before
    assert core.store.one("SELECT redacted FROM observations")["redacted"] == 0
    assert core.store.rows("SELECT * FROM outbox") == []


def test_latest_ledger_replay_erases_attributed_goal_but_keeps_independent_source(
    empty_database, core, tmp_path,
):
    enable_nightly(core)
    core.settings.goals_enabled = True
    suggestion, proposal = reflected_proposal(core)
    derived = core.goals.propose(proposal, reflection_suggestion_id=suggestion["suggestion_id"])
    ingest(core, 90, "独立的保留来源")
    independent = core.goals.propose(
        GoalProposal(topic="独立保留主题", source_event_ids=["evt_test_90"])
    )
    key, archive = Fernet.generate_key(), tmp_path / "growth.enc"
    restored_state = tmp_path / "restored-state"
    backup(core.settings.database, archive, key)
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [proposal.source_event_ids[0]], core.clock())
    restore(archive, empty_database, key, core.settings.erasure_ledger,
            state_directory=restored_state)
    restored = Core(core.settings.model_copy(update={
        "database": empty_database, "state_directory": str(restored_state),
    }), clock=core.clock)
    try:
        assert restored.reflections.recent() == []
        assert [row["goal_id"] for row in restored.goals.recent()] == [independent]
        row = restored.store.one('SELECT redacted FROM goals WHERE goal_id=%s', (derived,))
        assert row["redacted"]
        assert not restored.store.one(
            "SELECT 1 FROM pg_constraint WHERE connamespace=current_schema()::regnamespace "
            "AND NOT convalidated"
        )
    finally:
        restored.close()


def test_many_independent_reads_on_one_calendar_day_never_stabilize_interest(core):
    for number in range(1, 8):
        complete_read(core, number, f"https://example.org/{number}")
    item = view(core)["self.interest"][0]
    assert item["independent_sessions"] == 7 and item["independent_days"] == 1
    assert item["status"] == "EMERGING"
    assert all(row["evidence_level"] == "SIMULATED_FETCHED" for row in item["evidence"])
