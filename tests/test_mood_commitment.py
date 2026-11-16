"""A commitment that ran past its deadline leaves her a little at fault, once."""

import pytest

from tests.support.goals import setup_goal


def missed(core):
    core.settings.mood_enabled = True
    goal = setup_goal(core, commitment=True)
    deadline = core.store.one("SELECT deadline FROM commitments")["deadline"]
    return goal, deadline


def test_deadline_miss_is_one_bounded_signal_at_the_deadline(core):
    _, deadline = missed(core)
    assert core.mood.snapshot()["signals"] == []
    core.clock.advance(86401)
    assert core.mood.snapshot()["signals"] == []  # Not yet expired by Core.
    assert core.goals.expire_commitments() == 1
    view = core.mood.snapshot()
    [signal] = view["signals"]
    assert signal["reason"] == "COMMITMENT_MISSED" and signal["kind"] == "commitment"
    assert signal["event_id"] == "evt_test_1" and signal["at"] == deadline
    assert view["immediate"]["pleasure"] == pytest.approx(-.03, abs=1e-3)
    assert view["immediate"]["dominance"] == pytest.approx(-.02, abs=1e-3)
    core.clock.advance(12 * 3600)
    assert core.mood.snapshot()["immediate"]["pleasure"] == pytest.approx(-.015, abs=1e-3)


def test_other_failures_and_redacted_goals_do_not_count(core):
    goal, _ = missed(core)
    core.clock.advance(86401)
    core.goals.expire_commitments()
    core.store.execute("UPDATE commitments SET failure_code='STEP_FAILED'")
    assert core.mood.snapshot()["signals"] == []
    core.store.execute("UPDATE commitments SET failure_code='DEADLINE_EXCEEDED'")
    core.store.execute("UPDATE goals SET redacted=1 WHERE goal_id=%s", (goal,))
    assert core.mood.snapshot()["signals"] == []


def test_miss_follows_the_fourteen_day_mood_window(core):
    missed(core)
    core.clock.advance(86401)
    core.goals.expire_commitments()
    assert len(core.mood.snapshot()["signals"]) == 1
    core.clock.advance(15 * 86400)
    assert core.mood.snapshot()["signals"] == []
