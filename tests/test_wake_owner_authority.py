"""Real native goals cannot advance through a Wake purpose alone."""

import pytest

from companion.autonomous_selector import native_selector
from companion.goals import GoalProposal
from tests.conftest import ingest, run
from tests.support.owner_authority import activate
from tests.test_goals import setup_goal
from tests.test_observations import enable_observations
from tests.test_owner_authority import prepare


def invoke(core, entry):
    if entry == "direct":
        return bool(core.wake.evaluate())
    lane = next(lane for lane in native_selector(core, "wake-purpose").lanes
                if lane.name == "wake")
    return run(lane.run()).progressed


def source_before_grant(core):
    core.settings.goals_enabled = core.settings.exploration_enabled = True
    core.settings.wake_enabled = True
    enable_observations(core)
    ingest(core, 1, "请阅读这篇天文文章")
    core.store.execute("UPDATE jobs SET state='CANCELLED',lease_token=NULL WHERE state='QUEUED'")


def goal_after_grant(core):
    return core.goals.propose(GoalProposal(
        topic="了解天文内容", source_event_ids=["evt_test_1"],
        scope={"operation": "read_content", "url": "https://example.org/article"},
    ))


@pytest.mark.parametrize("entry", ["direct", "selector"])
@pytest.mark.parametrize("missing", ["wake", "goals", "exploration"])
def test_wake_missing_purpose_preserves_real_goal_and_steps(core, tmp_path, entry, missing):
    goal_id = setup_goal(core)
    policy = prepare(core, tmp_path).model_copy(update={
        "purposes": [p for p in ("wake", "goals", "exploration") if p != missing],
    })
    activate(core, policy)
    before = core.store.one("SELECT * FROM goals WHERE goal_id=?", (goal_id,))
    changes = core.store.db.total_changes
    assert not invoke(core, entry)
    assert core.store.one("SELECT * FROM goals WHERE goal_id=?", (goal_id,)) == before
    assert core.store.rows("SELECT * FROM goal_steps") == []
    assert core.store.rows("SELECT * FROM wake_decisions") == []
    assert core.store.db.total_changes == changes


@pytest.mark.parametrize("entry", ["direct", "selector"])
def test_authorized_wake_without_affect_does_not_read_it(core, tmp_path, monkeypatch, entry):
    source_before_grant(core)
    core.settings.affect_enabled = True
    policy = prepare(core, tmp_path).model_copy(update={
        "purposes": ["wake", "goals", "exploration"],
    })
    activate(core, policy)
    goal_id = goal_after_grant(core)

    def forbidden_snapshot():
        pytest.fail("Affect read without its independent purpose")

    monkeypatch.setattr(core.wake.affect, "snapshot", forbidden_snapshot)
    assert invoke(core, entry)
    assert core.goals.get(goal_id)["state"] == "QUEUED"
    assert core.store.one("SELECT goal_id,state FROM goal_steps") == {
        "goal_id": goal_id, "state": "QUEUED",
    }
    assert core.store.one("SELECT decision,reason_code FROM wake_decisions") == {
        "decision": "EXPLORE", "reason_code": "GOAL_DUE",
    }
    assert core.store.rows("SELECT * FROM model_usage") == []
    assert core.store.rows("SELECT * FROM outbox") == []


def test_topic_pause_allows_authorized_background_read(core, tmp_path):
    source_before_grant(core)
    core.settings.affect_enabled = True
    ingest(core, 2, "先别聊这个")
    core.store.execute("UPDATE jobs SET state='CANCELLED',lease_token=NULL WHERE state='QUEUED'")
    policy = prepare(core, tmp_path).model_copy(update={
        "purposes": ["wake", "goals", "exploration", "affect"],
    })
    activate(core, policy)
    goal_id = goal_after_grant(core)
    assert core.wake.affect.snapshot()["state"]["engagement"] == "pause"
    assert core.wake.evaluate()
    assert core.goals.get(goal_id)["state"] == "QUEUED"
    assert not core.store.rows("SELECT 1 FROM outbox")


def regrant(core, tmp_path):
    """Activate, keep a goal from that grant, then STOP and activate a later revision."""
    from companion import owner_authority
    from companion.owner_authority_protocol import AuthorityStop

    source_before_grant(core)
    policy = prepare(core, tmp_path).model_copy(update={
        "purposes": ["wake", "goals", "exploration"],
    })
    first, _ = activate(core, policy)
    stale = goal_after_grant(core)
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="wake-stale-stop", authority=first["authority"]))
    activate(core, policy, revision=2, operation="wake-stale-reactivate")
    return stale


@pytest.mark.parametrize("entry", ["direct", "selector"])
def test_goal_from_superseded_grant_is_closed_and_next_goal_runs(core, tmp_path, entry):
    stale = regrant(core, tmp_path)
    current = core.goals.propose(GoalProposal(
        topic="继续了解天文", source_event_ids=["evt_test_1"],
        scope={"operation": "read_content", "url": "https://example.org/next"},
    ))
    assert invoke(core, entry)
    assert core.goals.get(stale)["state"] == "FAILED"
    assert core.goals.get(stale)["last_error_code"] == "OWNER_AUTHORITY_REVISION_CHANGED"
    assert core.goals.get(current)["state"] == "QUEUED"
    assert core.store.one("SELECT decision,reason_code,goal_id FROM wake_decisions") == {
        "decision": "EXPLORE", "reason_code": "GOAL_DUE", "goal_id": current,
    }
    assert core.store.one("SELECT action FROM audit_log WHERE entity_id=? "
                          "AND action='goal.authority_rejected'", (stale,))


@pytest.mark.parametrize("discovery", [0, 3])
def test_only_superseded_goals_still_record_a_decision(core, tmp_path, discovery):
    stale = regrant(core, tmp_path)
    core.settings.exploration_daily_requests = discovery
    core.wake.evaluate()
    assert core.goals.get(stale)["state"] == "FAILED"
    assert core.store.one("SELECT decision,reason_code FROM wake_decisions") == (
        {"decision": "OBSERVE", "reason_code": "DISCOVERY_DUE"} if discovery
        else {"decision": "SKIP", "reason_code": "NO_CANDIDATE"})
    assert core.store.rows("SELECT * FROM goal_steps") == []
