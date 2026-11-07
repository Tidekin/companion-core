import copy

import pytest

from companion.affect import (
    NEUTRAL,
    RESET_KEY,
    apply_feedback_reset,
    feedback_reset_at,
    reset_record,
    validate_reset_record,
)
from companion.contracts import CoreError
from companion.privacy import epoch
from tests.conftest import ingest


def apply(core, request="owner-reset"):
    record = reset_record(1, request, core.clock())
    with core.store.transaction():
        result = apply_feedback_reset(core.store, record)
    return record, result


def test_reset_clears_old_feedback_and_growth_without_erasing_facts_or_permissions(core):
    core.settings.affect_enabled = True
    ingest(core, 1, "简短一点")
    before = {table: core.store.rows("SELECT * FROM " + table) for table in (
        "events", "memories", "traits", "goals", "outbox", "model_usage")}
    record, result = apply(core)
    assert result["state"] == "RESET"
    assert core.affect.snapshot()["state"] == NEUTRAL
    assert not core.affect.observe("evt_test_1")
    assert core.expression_learning.snapshot(core.settings.owner_qq_id)["items"] == []
    assert {table: core.store.rows("SELECT * FROM " + table) for table in before} == before
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key LIKE '%%paused'")
    assert core.model.calls == []
    old_epoch = epoch(core.store)
    with core.store.transaction():
        apply_feedback_reset(core.store, record)
    assert epoch(core.store) == old_epoch


def test_new_feedback_survives_replay_and_reset_cutoff_cannot_move_backward(core):
    core.settings.affect_enabled = True
    ingest(core, 1, "简短一点")
    first, _ = apply(core)
    core.clock.advance(1)
    ingest(core, 2, "详细解释")
    with core.store.transaction():
        apply_feedback_reset(core.store, first)
    assert core.affect.snapshot()["state"]["interaction_tone"] == "detailed"
    second, _ = apply(core, "second")
    with core.store.transaction():
        apply_feedback_reset(core.store, first)
    assert feedback_reset_at(core.store) == second["at"]
    assert core.affect.snapshot()["state"] == NEUTRAL


def test_restore_projection_replay_blocks_old_source_even_when_cached_rows_return(core):
    core.settings.affect_enabled = True
    ingest(core, 1, "简短一点")
    old = core.store.one("SELECT * FROM affect_evidence")
    record, _ = apply(core)
    with core.store.transaction():
        core.store.execute('DELETE FROM runtime_meta WHERE key=%s', (RESET_KEY,))
        core.store.insert("affect_evidence", **old)
        apply_feedback_reset(core.store, record)
    assert core.affect.snapshot()["state"] == NEUTRAL
    assert not core.affect.preview("evt_test_1")


@pytest.mark.parametrize("field,value", [("event_ids", ["evt_test_1"]),
    ("memory_ids", ["mem_any"]), ("memory_id", "memory:any"), ("at", "2026-10-25")])
def test_reset_record_cannot_become_a_source_erasure(core, field, value):
    record = reset_record(1, "owner-reset", core.clock())
    record[field] = value
    with pytest.raises(ValueError):
        validate_reset_record(record)


def test_owner_authorization_and_unintegrated_ledger_fail_before_changes(core):
    before = copy.deepcopy(core.store.rows("SELECT * FROM runtime_meta"))
    with pytest.raises(CoreError, match="OWNER_REQUIRED"):
        core.affect.reset("99999", "reset", core.ledger)
    with pytest.raises(CoreError, match="AFFECT_RESET_ID_REQUIRED"):
        core.affect.reset(core.settings.owner_qq_id, "", core.ledger)
    with pytest.raises(CoreError, match="AFFECT_RESET_NOT_INTEGRATED"):
        core.affect.reset(core.settings.owner_qq_id, "reset", object())
    assert core.store.rows("SELECT * FROM runtime_meta") == before


def test_reset_invalidates_prepared_policy_without_deleting_source(core):
    from companion.conversation_policy import ConversationPolicyConfig, decide, revalidate

    core.settings.affect_enabled = True
    ingest(core, 1, "简短一点")
    config = ConversationPolicyConfig(mode="ENABLED", strategy="participation_v1")
    snapshot = core.affect.snapshot()
    core.affect.revalidate(snapshot)
    receipt = decide(config, "怎么做", snapshot).receipt()
    apply(core)
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        core.affect.revalidate(snapshot)
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        revalidate(receipt, config, current_text="怎么做", affect=core.affect.snapshot())
    source = core.store.one("SELECT redacted FROM events WHERE event_id='evt_test_1'")
    assert source["redacted"] == 0
