"""Current confirmations must not rewrite memories for another business interval."""

import pytest

from companion.contracts import Candidate, CoreError
from companion.privacy import epoch
from companion.store import after
from tests.conftest import ingest
from tests.postgres_fixture import logical_state
from tests.test_m1 import remember
from tests.test_memory_workflows import message, reply


def set_inactive_interval(core, memory_id, interval):
    now = core.clock()
    bounds = ((after(now, -60), now) if interval == "expired"
              else (after(now, 60), after(now, 120)))
    core.store.execute('UPDATE memories SET valid_from=%s,valid_to=%s WHERE memory_id=%s',
                       (*bounds, memory_id))
    return core.store.one('SELECT * FROM memories WHERE memory_id=%s', (memory_id,))


@pytest.mark.parametrize("interval", ["expired", "future"])
def test_remember_command_creates_current_fact_without_rewriting_another_interval(core, interval):
    fact = "青禾项目配色是蓝色"
    assert message(core, 1, "/记住这个 " + fact)["state"] == "SUCCEEDED"
    old_id = core.store.one("SELECT memory_id FROM memories")["memory_id"]
    old = set_inactive_interval(core, old_id, interval)
    old_evidence = core.store.rows('SELECT * FROM memory_evidence WHERE memory_id=%s', (old_id,))
    assert core.memory_context.recall("*", core.clock())["state"] == "NO_RECORD"

    assert message(core, 2, "/记住这个 " + fact)["state"] == "SUCCEEDED"
    current = core.memory_context.recall("*", core.clock())["facts"]
    assert len(current) == 1
    new_id = current[0]["memory_id"]
    assert new_id != old_id and new_id in reply(core, 2)
    assert current[0]["status"] == "CONFIRMED"
    assert current[0]["valid_from"] is None and current[0]["valid_to"] is None
    assert current[0]["evidence_ids"] == ["evt_test_2"]

    # A later repeat must find the current row even when the older duplicate comes first.
    assert message(core, 3, "/记住这个 " + fact)["state"] == "SUCCEEDED"
    assert new_id in reply(core, 3)
    assert core.store.one("SELECT COUNT(*) n FROM memories")["n"] == 2
    assert message(core, 4, "/你记得什么")["state"] == "SUCCEEDED"
    assert new_id in reply(core, 4) and old_id not in reply(core, 4)
    assert core.store.one('SELECT * FROM memories WHERE memory_id=%s', (old_id,)) == old
    assert core.store.rows('SELECT * FROM memory_evidence WHERE memory_id=%s',
                           (old_id,)) == old_evidence
    assert not core.store.rows("SELECT * FROM memory_revisions")


def candidate(core, text):
    source = ingest(core, 2, text)
    return Candidate(subject="user", kind="fact", fact_key="project-color", content=text,
                     evidence_event_id=source["event_id"], evidence_quote=text)


@pytest.mark.parametrize("interval", ["expired", "future"])
@pytest.mark.parametrize("relation", ["same", "conflicts"])
def test_explicit_resolution_rejects_noncurrent_memory_without_any_write(core, interval, relation):
    old_id = remember(core, 1, "青禾项目配色是蓝色", fact_key="project-color", confirmed=True)
    proposed = candidate(core, "青禾项目使用蓝色")
    resolution = {"same": old_id if relation == "same" else None,
                  "conflicts": [old_id] if relation == "conflicts" else []}
    old = set_inactive_interval(core, old_id, interval)
    before = logical_state(core.store), epoch(core.store)
    with pytest.raises(CoreError, match="MEMORY_MATCH_STALE"), core.store.transaction(
        control_write=True
    ):
        core.memory.add(proposed, core.clock(), confirmed=True, resolution=resolution)
    assert (logical_state(core.store), epoch(core.store)) == before
    assert core.store.rows("SELECT * FROM memories") == [old]
    assert not core.store.rows("SELECT * FROM memory_evidence WHERE event_id='evt_test_2'")


@pytest.mark.parametrize("interval", ["expired", "future", "current"])
def test_default_conflict_applies_only_to_current_business_interval(core, interval):
    old_id = remember(core, 1, "青禾项目配色是蓝色", fact_key="project-color", confirmed=True)
    old = (set_inactive_interval(core, old_id, interval) if interval != "current"
           else core.store.one('SELECT * FROM memories WHERE memory_id=%s', (old_id,)))
    proposed = candidate(core, "青禾项目配色是红色")
    with core.store.transaction(control_write=True):
        new_id = core.memory.add(proposed, core.clock(), confirmed=True)
    rows = {row["memory_id"]: row for row in core.store.rows("SELECT * FROM memories")}
    assert new_id != old_id and len(rows) == 2
    assert rows[new_id]["needs_review"] == (interval == "current")
    assert rows[old_id]["needs_review"] == (interval == "current")
    view = core.memory_context.recall("*", core.clock())
    if interval == "current":
        assert {row["memory_id"] for row in view["conflicts"]} == {old_id, new_id}
    else:
        assert rows[old_id] == old
        assert [row["memory_id"] for row in view["facts"]] == [new_id]
        assert not view["conflicts"]


@pytest.mark.parametrize("explicit", [False, True])
def test_same_memory_at_inclusive_start_keeps_identity_and_business_interval(core, explicit):
    fact = "青禾项目配色是蓝色"
    old_id = remember(core, 1, fact, confirmed=True)
    bounds = core.clock(), after(core.clock(), 60)
    core.store.execute('UPDATE memories SET valid_from=%s,valid_to=%s WHERE memory_id=%s',
                       (*bounds, old_id))
    proposed = candidate(core, fact)
    with core.store.transaction(control_write=True):
        result = core.memory.add(proposed, core.clock(), confirmed=True,
                                 resolution={"same": old_id, "conflicts": []} if explicit else None)
    assert result == old_id
    assert core.store.rows("SELECT valid_from,valid_to FROM memories") == [
        {"valid_from": bounds[0], "valid_to": bounds[1]},
    ]
    assert core.memory_context.recall("*", core.clock())["facts"][0]["evidence_ids"] == [
        "evt_test_1", "evt_test_2",
    ]
