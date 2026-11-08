"""Autonomy uses scoped native receipts and never persists a parallel progress counter."""

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from companion import interest
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.life_context import decision_context
from companion.life_protocol import LifeDecision, LifeLoopConfig
from companion.store import encode
from tests.postgres_fixture import logical_state
from tests.support.goals import result_for, setup_goal
from tests.support.growth_foundations import complete_read


def context(core, ids):
    return decision_context(core.store, core.settings,
                            [{"event_id": key} for key in ids], "synthetic", core.clock())


def test_guidance_uses_only_current_source_closure_and_has_no_write_or_clock(core):
    core.settings.life_loop = LifeLoopConfig(autonomy_enabled=True)
    core.settings.memory_enabled = True
    complete_read(core, 1, "https://example.org/scoped")
    complete_read(core, 2, "https://example.org/unrelated")
    core.store.execute("UPDATE goals SET topic='其他私人兴趣' WHERE goal_id IN "
                       "(SELECT goal_id FROM goal_evidence WHERE event_id='evt_test_2')")
    revision = core.store.one("SELECT version_num FROM alembic_version")["version_num"]
    before = logical_state(core.store)
    for ids in (["evt_test_1"], ["obs_test_1"], ["evt_test_1", "obs_test_2"]):
        view = context(core, ids)
        assert view["self_interests"] == []
        assert view["autonomy_guidance"]["depth_control"] == []
    view = context(core, ["evt_test_1", "obs_test_1"])
    guidance = view["autonomy_guidance"]["depth_control"]
    assert len(guidance) == 1 and guidance[0]["independent_sessions"] == 1
    assert guidance[0]["source_event_ids"] == ["evt_test_1", "obs_test_1"]
    assert "其他私人兴趣" not in encode(view)
    core.clock.advance(60)
    assert context(core, ["evt_test_1", "obs_test_1"]) == view
    assert logical_state(core.store) == before
    assert core.store.one("SELECT version_num FROM alembic_version")["version_num"] == revision
    assert not core.store.one(
        "SELECT 1 FROM pg_constraint WHERE connamespace=current_schema()::regnamespace "
        "AND NOT convalidated"
    )


@pytest.mark.parametrize("mutation", ["failed", "unknown", "withdrawn", "discovered", "empty_hash"])
def test_unfinished_or_invalid_receipts_never_supply_guidance(core, mutation):
    core.settings.life_loop = LifeLoopConfig(autonomy_enabled=True)
    core.settings.memory_enabled = True
    goal = complete_read(core, 1, "https://example.org/scoped")
    if mutation in {"failed", "unknown"}:
        core.store.execute('UPDATE goals SET state=%s WHERE goal_id=%s',
                           ("FAILED" if mutation == "failed" else "WAITING_INPUT", goal))
    elif mutation == "withdrawn":
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    elif mutation == "discovered":
        core.store.execute("UPDATE observations SET result_status='DISCOVERED'")
    else:
        core.store.execute("UPDATE observations SET source_sha256=''")
    view = context(core, ["evt_test_1", "obs_test_1"])
    assert view["self_interests"] == []
    assert view["autonomy_guidance"]["depth_control"] == []


def test_interest_provenance_and_completion_commit_atomically(core, monkeypatch):
    goal = setup_goal(core)
    core.goals.accept(goal)
    step = core.goals.claim()
    result = result_for(core, goal)
    original = interest.record_exploration

    def interrupted(store, *args):
        original(store, *args)
        raise RuntimeError("synthetic interruption after interest write")

    monkeypatch.setattr(interest, "record_exploration", interrupted)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        core.goals.finish(step, result)
    assert core.goals.get(goal)["state"] == "RUNNING"
    assert core.store.one("SELECT state FROM goal_steps")["state"] == "RUNNING"
    assert core.store.rows("SELECT * FROM trait_evidence") == []
    assert core.store.rows("SELECT * FROM traits") == []
    monkeypatch.setattr(interest, "record_exploration", original)
    assert core.goals.finish(step, result)["state"] == "DONE"
    with pytest.raises(CoreError, match="STALE_LEASE"):
        core.goals.finish(step, result)
    assert len(core.store.rows("SELECT * FROM trait_evidence")) == 1


def test_latest_erasure_ledger_removes_guidance_from_an_older_backup(
    empty_database, core, tmp_path,
):
    core.settings.life_loop = LifeLoopConfig(autonomy_enabled=True)
    core.settings.memory_enabled = True
    complete_read(core, 1, "https://example.org/scoped")
    ids = ["evt_test_1", "obs_test_1"]
    assert context(core, ids)["autonomy_guidance"]["depth_control"]
    archive = tmp_path / "before.enc"
    restored_state = tmp_path / "restored-state"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [ids[0]], core.clock())
    assert context(core, ids)["autonomy_guidance"]["depth_control"] == []
    restore(archive, empty_database, key, core.settings.erasure_ledger,
            state_directory=restored_state)
    with Core(core.settings.model_copy(update={
        "database": empty_database, "state_directory": str(restored_state),
    }),
              clock=core.clock) as restored:
        assert context(restored, ids)["autonomy_guidance"]["depth_control"] == []
        assert restored.store.rows("SELECT * FROM trait_evidence") == []


@pytest.mark.parametrize("destination", [
    "PRIVATE_MESSAGE_DRAFT", "QZONE_DRAFT", "PRIVATE_CREATION",
])
def test_interest_reflection_is_confined_to_private_diary(destination):
    with pytest.raises(ValidationError, match="private diary destination"):
        LifeDecision(action="SHARE", destination=destination, source_event_ids=["receipt"],
                     reason="一次阅读后的想法", text="内容想法", interest_shift="兴趣变化想法")
