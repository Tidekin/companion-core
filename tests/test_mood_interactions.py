"""Daily discussion appraisals reuse delivered native turns, never model self-report."""

from contextlib import closing

import pytest

from companion.contracts import CoreError
from companion.memory import Memory
from companion.mood import MoodService
from companion.store import Store, encode
from tests.conftest import ingest
from tests.support.mood import erase, feedback


@pytest.mark.parametrize("text,reason,delta", [
    ("这个想法挺有意思！", "SHARED_IDEA_INTEREST", [.025, .02, 0]),
    ("这个想法很有意思，我们继续聊聊。", "SHARED_IDEA_INTEREST", [.025, .02, 0]),
    ("这个问题值得继续探讨", "DISCUSSION_CURIOSITY", [.01, .025, 0]),
    ("我对这个问题很好奇，想再听听你的看法", "DISCUSSION_CURIOSITY", [.01, .025, 0]),
    ("这个角度我真没想到", "PERSPECTIVE_SURPRISE", [0, .03, 0]),
    ("这个思路让我眼前一亮", "PERSPECTIVE_SURPRISE", [.02, .03, 0]),
    ("这个观点我不认同", "VIEW_DISAGREEMENT", [0, .01, 0]),
    ("我不太同意这个看法，我们继续聊聊", "VIEW_DISAGREEMENT", [0, .01, 0]),
    ("这样就说得通了", "DISCUSSION_PROGRESS", [.025, 0, .025]),
    ("我们把这个问题说清楚了", "DISCUSSION_PROGRESS", [.025, 0, .025]),
])
def test_explicit_daily_appraisals_have_small_source_bound_impulses(core, text, reason, delta):
    key = feedback(core, text=text)
    changes = core.store.db.total_changes
    view = core.mood.snapshot()
    assert view["version"] == "mood-2" and view["confidence"] == "limited"
    assert len(view["signals"]) == 1
    signal = view["signals"][0]
    assert signal["kind"] == "interaction" and signal["reason"] == reason
    assert signal["event_id"] == key and signal["delta"] == delta
    assert signal["source_event_ids"] == ["evt_test_2", key]
    assert list(view["immediate"].values()) == delta
    assert not any(view["undertone"].values())
    assert core.store.db.total_changes == changes and core.model.calls == []


@pytest.mark.parametrize("text", [
    "我很兴奋", "我很好奇", "你很兴奋", "小肥鱼说自己很兴奋", "我今天很开心",
    "你不理我了", "好久没联系了", "简短一点", "谢谢", "这个想法很有意思吗",
    "这个想法没意思", "这个想法不是很有意思", "假设这个想法很有意思",
    "如果这个想法很有意思，我们继续聊聊", "他说：这个想法很有意思",
    "“这个想法很有意思”", "小说里她说这个角度我真没想到", "扮演我说这样就说得通了",
    "请记住我说这个问题值得继续探讨", "这个想法很有意思，但是我不想继续讨论",
    "这个观点我不认同，所以你应该难过", "我讨厌你", "我们明天会把这个问题说清楚",
])
def test_no_substring_guessing_fiction_emotion_transfer_or_silence_inference(core, text):
    feedback(core, text=text)
    assert core.mood.snapshot()["signals"] == []
    core.clock.advance(48 * 3600)
    assert not any(core.mood.snapshot()["expression"].values())


@pytest.mark.parametrize("damage", [
    "quote", "rich", "media", "shared_content", "control_command", "diary_command",
    "actor", "group",
    "PENDING", "SENDING", "DELIVERY_UNCERTAIN", "CANCELLED", "simulated",
])
def test_appraisals_require_unambiguous_original_private_owner_input_and_delivery(core, damage):
    key = feedback(core, text="这个想法很有意思")
    if damage in {"quote", "rich", "media", "shared_content", "control_command", "diary_command"}:
        core.store.execute("UPDATE events SET body_json=? WHERE event_id=?", (
            encode({"text": "这个想法很有意思", damage: {"text": "引用或指令"}}), key,
        ))
    elif damage == "actor":
        core.store.execute("UPDATE events SET actor_id='companion' WHERE event_id=?", (key,))
    elif damage == "group":
        core.store.execute("UPDATE conversations SET scope='group'")
    elif damage == "simulated":
        core.store.execute("UPDATE events SET source='observation.simulation' "
                           "WHERE event_id='evt_test_2'")
    else:
        core.store.execute("UPDATE outbox SET state=?", (damage,))
    assert core.mood.snapshot()["signals"] == []


@pytest.mark.parametrize("intervening_type", ["message.received", "message.media_received"])
def test_deictic_appraisal_does_not_guess_across_unanswered_topic_or_other_conversation(
    core, intervening_type,
):
    key = feedback(core, text="先换个话题")
    core.store.execute("UPDATE events SET event_type=? WHERE event_id=?", (intervening_type, key))
    core.clock.advance(1)
    ingest(core, 10, "这个想法很有意思")
    assert core.mood.snapshot()["signals"] == []
    ingest(core, 11, "这个想法很有意思",
           origin=f"alternate:FriendMessage:{core.settings.owner_qq_id}")
    assert core.mood.snapshot()["signals"] == []


def test_delayed_appraisal_is_not_bound_to_stale_reply_but_existing_mood_keeps_decaying(core):
    key = feedback(core, text="这个角度我真没想到")
    before = core.mood.snapshot()
    core.clock.advance(3601)
    # Simulate an actually later event, rather than the age of reading a snapshot.
    core.store.execute("UPDATE events SET occurred_at=?,ingested_at=? WHERE event_id=?",
                       (core.clock(), core.clock(), key))
    assert core.mood.snapshot()["signals"] == []
    # A valid old appraisal itself is retained and decays for the full normal window.
    core.store.execute("UPDATE events SET occurred_at=?,ingested_at=? WHERE event_id=?",
                       (before["evaluated_at"], before["evaluated_at"], key))
    assert core.mood.snapshot()["expression"]["arousal"] == pytest.approx(
        .03 * 2 ** (-3601 / 10800), abs=1e-6,
    )


def test_same_delivered_reply_cannot_accumulate_different_feedback_categories(core):
    feedback(core, text="这个想法很有意思")
    core.clock.advance(1)
    ingest(core, 10, "这个回答很有帮助")
    view = core.mood.snapshot()
    assert len(view["signals"]) == 1
    assert view["signals"][0]["reason"] == "HELPFUL_FEEDBACK"
    assert view["expression"]["pleasure"] == .08
    assert not any(view["undertone"].values())


def test_daily_sequence_conflicts_decays_and_rebuilds_without_model_or_writes(core):
    feedback(core, 1, "这个想法很有意思")
    feedback(core, 2, "这个问题让我很好奇")
    two = core.mood.snapshot()
    assert not any(two["undertone"].values())
    feedback(core, 3, "这个观点我不认同")
    three = core.mood.snapshot()
    # Disagreement is engagement, not evidence of rejection or hurt.
    assert three["expression"]["pleasure"] > 0
    assert three["undertone"]["pleasure"] == 0
    assert three["undertone"]["arousal"] > 0
    assert three["immediate"]["arousal"] > two["immediate"]["arousal"]
    feedback(core, 4, "这次讨论有进展了")
    four = core.mood.snapshot()
    assert four["expression"]["dominance"] > 0
    feedback(core, 5, "这个回答让我有点失望")
    conflicting = core.mood.snapshot()
    assert conflicting["expression"]["pleasure"] < four["expression"]["pleasure"]
    changes = core.store.db.total_changes
    core.clock.advance(3 * 3600)
    decayed = core.mood.snapshot()
    for dimension in ("pleasure", "arousal", "dominance"):
        assert decayed["immediate"][dimension] == pytest.approx(
            conflicting["immediate"][dimension] / 2, abs=1e-6,
        )
    core.clock.advance(27 * 3600)
    current = core.mood.snapshot()
    with closing(Store(core.settings.database)) as reopened:
        service = MoodService(Memory(reopened, core.settings, core.ledger), core.clock)
        assert service.snapshot() == current
    assert core.store.db.total_changes == changes and core.model.calls == []


@pytest.mark.parametrize("source", ["evt_test_2", "evt_test_3"])
def test_forgetting_either_side_removes_the_signal_and_invalidates_frozen_proof(core, source):
    feedback(core, text="这样就说得通了")
    view = core.mood.snapshot()
    assert view["signals"]
    erase(core, [source])
    assert core.mood.snapshot()["signals"] == []
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        core.mood.revalidate(view)


def test_repeated_daily_turns_saturate_and_neutral_chat_does_not_evict_history(core):
    for number in range(1, 25):
        feedback(core, number, "这个思路让我眼前一亮")
    view = core.mood.snapshot()
    assert 0 < view["immediate"]["arousal"] < .35
    assert 0 < view["undertone"]["arousal"] < .15
    for number in range(100, 380):
        ingest(core, number, "普通日常聊天")
    assert core.mood.snapshot() == view


def test_scoped_consumers_need_both_sides_of_shared_discussion(core):
    key = feedback(core, text="这个想法很有意思")
    assert core.mood.reference(allowed=[key]) is None
    reference = core.mood.reference(allowed=[key, "evt_test_2"])
    assert reference["state"]["pleasure"] > 0
    assert "主人明确偏好" in reference["rules"]
    assert core.mood.reference(text="认真说，家人住院了") is None
