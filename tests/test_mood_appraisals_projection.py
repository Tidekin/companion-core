"""Settled appraisals join the native, bounded and reproducible mood projection."""

from datetime import datetime

import pytest

from companion import mood, mood_appraisals
from companion.contracts import InteractionSignal
from tests.conftest import ingest
from tests.support.mood import erase, feedback
from tests.support.mood_appraisals import appraise


def test_mood_appraisals_integrate_with_existing_signals(core):
    feedback(core, text="这个想法很有意思")
    appraise(core, 10, "想你了")
    view = core.mood.snapshot()
    assert {s["reason"] for s in view["signals"]} == {
        "SHARED_IDEA_INTEREST", "OWNER_AFFECTION",
    }
    assert {s["kind"] for s in view["signals"]} == {"interaction", "appraisal"}


def test_appraisals_obey_fourteen_day_window(core):
    appraise(core, 1, "旧记录", "OWNER_PLAYFUL")
    assert len(core.mood.snapshot()["signals"]) == 1
    core.clock.advance(15 * 86400)
    assert core.mood.snapshot()["signals"] == []


def test_appraisals_respect_shared_candidate_limit(core, monkeypatch):
    monkeypatch.setattr(mood, "LIMIT", 4)
    for number in range(1, 4):
        feedback(core, number, text="这个想法很有意思")
        core.clock.advance(1)
    appraise(core, 100, "玩笑一", "OWNER_PLAYFUL")
    core.clock.advance(1)
    appraise(core, 101, "玩笑二", "OWNER_PLAYFUL")
    view = core.mood.snapshot()
    assert view["truncated"] is True
    assert len(view["signals"]) == 4
    assert view["signals"][-1]["event_id"] == "evt_test_101"
    assert not any(view["undertone"].values())


def test_appraisals_decay_with_half_life(core):
    appraise(core, 1, "开玩笑", "OWNER_PLAYFUL")
    assert core.mood.snapshot()["immediate"]["pleasure"] == pytest.approx(.03)
    core.clock.advance(3 * 3600)
    assert core.mood.snapshot()["immediate"]["pleasure"] == pytest.approx(.015)


@pytest.mark.parametrize("damage", ["erase", "quote", "usage", "scenario"])
def test_still_valid_filters_invalid_appraisals_from_projection(core, damage):
    key = appraise(core, 1, "测试")
    assert len(core.mood.snapshot()["signals"]) == 1
    if damage == "erase":
        erase(core, [key])
    elif damage == "quote":
        core.store.execute("UPDATE events SET body_json='{}' WHERE event_id=?", (key,))
    elif damage == "usage":
        core.store.execute("UPDATE model_usage SET state='UNKNOWN'")
    else:
        core.store.execute("UPDATE model_usage SET scenario='chat'")
    assert core.mood.snapshot()["signals"] == []


def test_capped_enforcement_during_projection(core):
    core.clock.now = datetime.fromisoformat("2026-11-01T02:00:00+00:00")
    keys = []
    for number in range(1, 4):
        keys.append(appraise(core, number, f"想你{number}"))
        core.clock.advance(1)
    assert len(core.store.rows("SELECT * FROM mood_appraisals")) == 3
    assert [s["event_id"] for s in core.mood.snapshot()["signals"]] == keys[:2]
    other = appraise(core, 4, "逗你玩", "OWNER_PLAYFUL")
    assert [s["event_id"] for s in core.mood.snapshot()["signals"]] == [*keys[:2], other]


def test_appraisals_have_correct_signal_structure(core):
    key = appraise(core, 1, "想你")
    signal = core.mood.snapshot()["signals"][0]
    assert signal["kind"] == "appraisal" and signal["reason"] == "OWNER_AFFECTION"
    assert signal["event_id"] == key and signal["source_event_ids"] == [key]
    assert signal["delta"] == [.05, .02, .01]


def test_appraisals_contribute_to_immediate_and_undertone(core):
    feedback(core, text="这个想法很有意思")
    core.clock.advance(3600)
    appraise(core, 10, "想你了")
    core.clock.advance(3600)
    feedback(core, 2, text="这样就说得通了")
    view = core.mood.snapshot()
    assert len(view["signals"]) == 3
    assert any(view["immediate"].values()) and any(view["undertone"].values())


def test_deterministic_signal_wins_for_the_same_owner_event(core):
    key = feedback(core, text="这个想法很有意思")
    core.store.execute(
        "INSERT INTO model_usage(request_id,scenario,provider,model,state,occurred_at) "
        "VALUES ('duplicate','memory_extract','synthetic','synthetic','SETTLED',?)",
        (core.clock(),),
    )
    signals = mood_appraisals.validate(core.store, [InteractionSignal(
        event_id=key, reason="OWNER_AFFECTION", quote="这个想法很有意思")], {key})
    with core.store.transaction():
        mood_appraisals.record(core.store, core.settings, signals, "duplicate", core.clock())
    assert [s["reason"] for s in core.mood.snapshot()["signals"]] == ["SHARED_IDEA_INTEREST"]


def test_neutral_chat_does_not_reset_appraisal(core):
    appraise(core, 1, "想你了")
    before = core.mood.snapshot()["expression"]
    ingest(core, 2, "继续聊一聊")
    assert core.mood.snapshot()["expression"] == before
