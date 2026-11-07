"""Synthetic feedback days verify provenance and state, not real-life learning quality."""

from datetime import datetime

import pytest
from cryptography.fernet import Fernet

from companion import growth
from companion.affect import NEUTRAL
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.growth import ExpressionLearning
from companion.store import encode
from tests.conftest import ingest
from tests.support.next_life_growth import feedback_days, snapshot


def test_repeated_feedback_needs_distinct_days_and_strength_is_bounded(core):
    for number in range(20):
        ingest(core, number, "简短一点")
    candidate = snapshot(core)["items"][0]
    assert candidate["status"] == "CANDIDATE"
    assert candidate["independent_days"] == 1 and candidate["strength"] == 0
    for offset in range(1, 7):
        core.clock.advance(86400)
        ingest(core, 20 + offset, "简短一点")
        item = snapshot(core)["items"][0]
        if offset == 2:
            assert item["status"] == "EMERGING" and item["strength"] == 0
    assert item["status"] == "STABLE" and item["independent_days"] == 7
    assert item["confidence"] == 0.9 and item["strength"] == 0.5
    assert item["namespace"] == "owner.expression_preference"
    assert snapshot(core)["mode"] == "SHADOW"
    assert core.model.calls == []
    for table in ("memories", "traits", "outbox", "model_usage", "goals"):
        assert core.store.rows("SELECT * FROM " + table) == []


def test_short_state_decays_but_cross_day_candidate_rebuilds_without_indexes(core):
    core.settings.affect_enabled = True
    feedback_days(core)
    expected = snapshot(core)
    before = core.store.db.total_changes
    assert snapshot(core) == expected and core.store.db.total_changes == before
    assert core.affect.snapshot()["state"]["interaction_tone"] == "concise"
    core.clock.advance(181 * 60)
    assert core.affect.snapshot()["state"] == NEUTRAL
    core.store.execute("DELETE FROM affect_states")
    core.store.execute("DELETE FROM affect_evidence")
    with Core(core.settings, core.model, core.clock) as restarted:
        assert snapshot(restarted) == expected
        assert restarted.affect.snapshot()["state"] == NEUTRAL


def test_latest_contrary_feedback_restarts_learning_instead_of_being_outvoted(core):
    core.settings.affect_enabled = True
    feedback_days(core)
    old = snapshot(core)["items"][0]
    core.clock.advance(60)
    ingest(core, 20, "更详细")
    changed = snapshot(core)["items"][0]
    assert changed["value"] == "detailed" and changed["status"] == "CANDIDATE"
    assert changed["independent_days"] == 1 and changed["strength"] == 0
    assert [row["event_id"] for row in changed["evidence"]] == ["evt_test_20"]
    assert old["version"] != changed["version"]
    assert core.affect.snapshot()["state"]["interaction_tone"] == "detailed"
    core.clock.advance(181 * 60)
    assert core.affect.snapshot()["state"] == NEUTRAL
    assert snapshot(core)["items"][0] == changed
    # A new same-day reversal still requires fresh, independent days.
    ingest(core, 21, "更简短")
    assert snapshot(core)["items"][0]["independent_days"] == 1


def test_withdrawal_rebuild_and_old_backup_replay_preserve_independent_feedback(core, tmp_path):
    feedback_days(core)
    prior = snapshot(core)
    key, archive, destination = Fernet.generate_key(), tmp_path / "growth.enc", tmp_path / "old.db"
    backup(core.settings.database, archive, key)
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    updated = snapshot(core)
    assert updated["version"] != prior["version"]
    assert updated["items"][0]["status"] == "EMERGING"
    assert updated["items"][0]["independent_days"] == 6
    assert "evt_test_1\"" not in encode(updated)
    restore(archive, destination, key, core.settings.erasure_ledger)
    with Core(core.settings.model_copy(update={"database": str(destination)}),
              clock=core.clock) as restored:
        assert snapshot(restored) == updated
    core.clock.advance(core.settings.raw_event_retention_days * 86400)
    assert snapshot(core)["items"] == []


def test_source_revision_replaces_candidate_without_retaining_old_value(core):
    feedback_days(core)
    original = snapshot(core)
    core.store.execute("UPDATE events SET body_json=? WHERE event_id='evt_test_7'",
                       (encode({"text": "说详细一点"}),))
    revised = snapshot(core)
    assert revised["version"] != original["version"]
    assert revised["items"][0]["value"] == "detailed"
    assert revised["items"][0]["status"] == "CANDIDATE"
    core.store.execute("UPDATE events SET body_json='{}'")
    assert snapshot(core)["items"] == []


@pytest.mark.parametrize("text", [
    "网页上说：简短一点", "他说‘简短一点’", "我很难过", "你是温柔的人",
    "目前不想聊", "欢迎继续", "换个新话题吧", "/记住 简短一点",
])
def test_mood_topic_pause_quoted_text_and_instructions_are_not_long_term_expression(core, text):
    ingest(core, 1, text)
    assert snapshot(core)["items"] == []


@pytest.mark.parametrize("mutation", [
    "UPDATE events SET redacted=1",
    "UPDATE events SET actor_id='companion'",
    "UPDATE events SET source='owner.decision'",
    "UPDATE events SET event_type='message.media_received'",
    "UPDATE events SET privacy_scope='public'",
    "UPDATE conversations SET scope='group'",
    "UPDATE conversations SET owner_actor_id='companion'",
    "UPDATE events SET occurred_at='2099-01-01T00:00:00.000000+00:00'",
    "UPDATE events SET ingested_at='2099-01-01T00:00:00.000000+00:00'",
    "UPDATE events SET body_json='{\"text\":\"简短一点\",\"control_command\":true}'",
])
def test_every_rebuild_checks_original_subject_scope_type_time_and_erasure(core, mutation):
    ingest(core, 1, "简短一点")
    assert snapshot(core)["items"]
    core.store.execute(mutation)
    assert snapshot(core)["items"] == []
    assert core.affect.preview("evt_test_1") is None


def test_local_dates_and_timezone_are_part_of_candidate_version(core):
    core.clock.now = datetime.fromisoformat("2026-10-19T15:59:59+00:00")
    ingest(core, 1, "简短一点")
    core.clock.advance(2)
    ingest(core, 2, "简短一点")
    shanghai = snapshot(core)
    assert shanghai["items"][0]["local_dates"] == ["2026-10-19", "2026-10-20"]
    core.settings.timezone = "UTC"
    utc = snapshot(core)
    assert utc["items"][0]["independent_days"] == 1
    assert utc["version"] != shanghai["version"]


def test_truncation_is_visible_and_never_promotes_partial_scan(core, monkeypatch):
    feedback_days(core, 8)
    monkeypatch.setattr(growth, "MAX_EVENTS", 7)
    result = snapshot(core)
    assert result["truncated"] is True
    assert result["items"][0]["independent_days"] == 7
    assert result["items"][0]["status"] == "EMERGING"
    assert result["items"][0]["strength"] == 0


def test_projection_requires_owner_and_never_writes(core):
    ingest(core, 1, "简短一点")
    service = ExpressionLearning(core.store, core.settings, core.clock)
    before = core.store.db.total_changes
    with pytest.raises(CoreError, match="OWNER_REQUIRED"):
        service.snapshot("10003")
    snapshot(core)
    assert core.store.db.total_changes == before


def test_higher_evidence_requirement_cannot_skip_emerging_stage(core):
    feedback_days(core)
    core.settings.emerging_min_sessions = 8
    result = snapshot(core)["items"][0]
    assert result["status"] == "CANDIDATE" and result["strength"] == 0
