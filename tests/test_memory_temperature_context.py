"""Attention affects selected context while evidence remains authoritative."""

from companion.memory_context import MemoryContext
from tests.conftest import ingest
from tests.postgres_fixture import logical_state
from tests.support.confirmed_memory import remember
from tests.support.goals import setup_goal
from tests.support.next_memory_context import episode


def read(core, **kwargs):
    conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id='evt_test_1'")
    return MemoryContext(core.memory).continuation(
        conversation["conversation_id"], core.clock(), **kwargs,
    )["items"]


def test_old_active_goal_enters_context_and_cools_after_closure(core):
    goal = setup_goal(core)
    core.settings.memory_enabled = True
    eid = episode(core, 1)
    core.goals.accept(goal)
    core.clock.advance(8 * 86400)
    # Recent unrelated episodes must not push the old Hot candidate out of its lane.
    for number in range(2, 25):
        ingest(core, number, f"近期普通聊天 {number}")
        episode(core, number)
    items = read(core)
    assert items[0]["episode_id"] == eid
    assert items[0]["temperature"] == "Hot"
    core.goals.cancel(goal)
    assert eid not in {item["episode_id"] for item in read(core)}


def test_cold_selected_memory_reactivates_only_this_context(core):
    mid = remember(core, 1, "松林项目选用绿色封面", confirmed=True)
    eid = episode(core, 1)
    core.clock.advance(8 * 86400)
    assert not read(core)
    before = logical_state(core.store)
    with core.store.read_snapshot():
        item = read(core, used_memory_ids=[mid])[0]
        assert item["episode_id"] == eid
        assert item["attention_reasons"] == ["USED_IN_CONTEXT"]
        assert not read(core)
    assert logical_state(core.store) == before
    core.memory.decide(mid, "forget", core.clock())
    assert not read(core, used_memory_ids=[mid])


def test_hot_context_requires_whole_episode_authorization(core):
    goal = setup_goal(core)
    core.settings.memory_enabled = True
    ingest(core, 2, "一起讨论的另一个来源")
    episode(core, 1, 2)
    core.goals.accept(goal)
    core.clock.advance(8 * 86400)
    assert not read(core, event_ids=["evt_test_1"])
    assert not read(core, source_check=lambda key: key != "evt_test_2")
    assert read(core, event_ids=["evt_test_1", "evt_test_2"])


def test_current_event_and_unknown_execution_reactivate_context(core):
    goal = setup_goal(core)
    core.settings.memory_enabled = True
    episode(core, 1)
    core.clock.advance(8 * 86400)
    assert read(core, current_event_ids=["evt_test_1"])[0]["temperature"] == "Hot"
    core.goals.accept(goal)
    core.goals.claim()
    core.store.execute("UPDATE goals SET state='FAILED' WHERE goal_id=%s", (goal,))
    core.store.execute((
        "UPDATE goal_steps SET state='EXECUTION_UNCERTAIN' WHERE goal_id=%s"
    ), (goal,))
    assert read(core)[0]["attention_reasons"] == ["UNRESOLVED_GOAL"]
    core.store.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_1'")
    assert not read(core)


def test_closed_goal_history_cannot_hide_old_episode_active_goal(core, monkeypatch):
    from companion import goals
    from companion.goals import GoalProposal
    from companion.memory_projections import MemoryProjections

    original_uid = goals.uid
    keys = iter(f"goal_{number:032d}" for number in range(26))
    monkeypatch.setattr(goals, "uid", lambda prefix: next(keys) if prefix == "goal"
                        else original_uid(prefix))
    first = setup_goal(core)
    core.goals.cancel(first)
    for number in range(1, 25):
        closed = core.goals.propose(GoalProposal(
            topic=f"已经结束的历史目标 {number}", source_event_ids=["evt_test_1"]))
        core.goals.cancel(closed)
    active = core.goals.propose(GoalProposal(
        topic="仍需继续的目标", source_event_ids=["evt_test_1"]))
    core.goals.accept(active)
    core.settings.memory_enabled = True
    eid = episode(core, 1)
    core.clock.advance(8 * 86400)
    projected = MemoryProjections(core.memory).snapshot(
        core.clock(), episode_ids=[eid])["episodes"][0]
    assert len(projected["goals"]) == 20 and projected["goals_truncated"]
    assert projected["goals"][0]["goal_id"] == active
    assert read(core)[0]["episode_id"] == eid
    core.goals.cancel(active)
    assert not read(core)
