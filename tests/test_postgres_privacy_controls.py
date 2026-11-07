"""Native derived-memory correction and owner controls preserve erasure custody."""

from companion.privacy import epoch
from companion.store import after
from tests.conftest import run
from tests.test_diary import DAY, seed_day


def test_native_diary_generation_correction_and_forget(core):
    seed_day(core)
    core.diary.schedule_day(DAY)
    assert run(core.diary.generate_once("native-diary"))
    entry = core.diary.get_day(DAY, core.settings.owner_qq_id)
    assert entry and core.store.rows("SELECT event_id FROM diary_evidence")
    changed = core.diary.correct(DAY, "今天想保留的是读书计划", core.settings.owner_qq_id)
    assert changed["version"] == entry["version"] + 1
    assert "今天想保留的是读书计划" in changed["body_md"]
    assert core.diary.forget(DAY, core.settings.owner_qq_id)["status"] == "DELETED"
    assert core.diary.get_day(DAY, core.settings.owner_qq_id) is None
    assert not core.store.rows("SELECT * FROM diary_evidence")
    assert all(row["state"] == "SETTLED" for row in core.store.rows(
        "SELECT state FROM model_usage"))


def test_native_mood_clear_keeps_one_durable_reset_on_repeat(core):
    owner = core.settings.owner_qq_id
    assert core.mood.control("pause", owner)["state"] == "PAUSED"
    assert core.mood.control("resume", owner)["state"] == "ACTIVE"
    first = core.mood.control("clear", owner, request_id="native-reset")
    assert first["state"] == "RESET"
    assert core.mood.control("clear", owner, request_id="native-reset") == first
    resets = [row for row in core.ledger.read()[1:]
              if row.get("target_type") == "mood_reset"]
    assert len(resets) == 1 and resets[0]["memory_id"] == "mood_reset:native-reset"


def test_native_consent_revoke_advances_epoch_only_once(core):
    core.store.insert("consents", consent_id="native-consent", capabilities_json="[]",
                      device_id="synthetic-device", session_id="synthetic-session",
                      created_at=core.clock(), expires_at=after(core.clock(), 300))
    initial = epoch(core.store)
    service = core.observations.consents
    assert service.revoke("native-consent")["revoked"]
    assert epoch(core.store) == initial + 1
    service.revoke("native-consent")
    assert epoch(core.store) == initial + 1
