"""Chronological, synthetic replay. The provider is fixed; labels measure eligibility."""

import json

import pytest

from companion.conversation_policy import ConversationPolicyConfig
from companion.life_protocol import LifeLoopConfig
from companion.social import feedback_paused, proactive_counts
from tests.conftest import ingest, run
from tests.test_notices import ready_share
from tests.test_turn_taking import Model, turn

POINT = "昨天那个导出问题，我想到可以看看文件名里的空格。"
ROOT = "导出文件的问题明天再聊，可能和文件名有关"
BREAKFAST = "早餐店的油条刚出锅"
BUSY = "我去上课了，晚点聊"


def setup(core):
    core.settings.wake_enabled = core.settings.proactive_enabled = True
    core.settings.affect_enabled = True
    core.settings.proactive_idle_seconds = 0
    core.settings.quiet_start = core.settings.quiet_end = 0
    core.settings.life_loop = LifeLoopConfig(mode="SHADOW", follow_up_delay_seconds=120,
                                             min_interval_seconds=60)


def owner(core, number, text):
    ingest(core, number, text)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE kind='chat'")
    core.life_topics.synchronize()


# Each case is an independent conversation with the exact same candidate. Labels
# are authored windows for these fixtures. No later owner reply enters the prompt.
@pytest.mark.parametrize(("events", "check_at", "eligible"), [
    ([], 119, False),
    ([], 120, True),
    ([(30, BREAKFAST), (60, "小区里的桂花开了")], 120, True),
    ([(30, "导出文件的问题又出现了")], 120, False),
    ([(30, "先别聊这个")], 120, False),
    ([(30, BREAKFAST), (60, "先别聊这个")], 120, True),
    ([(30, BUSY)], 120, False),
    ([(30, BUSY)], 30 + 180 * 60 + 1, True),
    ([(30, "今天别主动找我")], 4 * 3600, False),
    ([(30, "/暂停自主活动")], 120, False),
], ids=["early", "due", "other-topics", "same-topic-update", "pause-this", "pause-other",
        "in-class", "after-class", "today", "all-autonomy"])
def test_fixed_candidate_timing(core, events, check_at, eligible, record_property):
    setup(core)
    owner(core, 1, ROOT)
    at = 0
    for number, (stamp, text) in enumerate(events, 2):
        core.clock.advance(stamp - at)
        if text.startswith("/"):
            ingest(core, number, text)
            assert run(core.run_once("owner-command"))
        else:
            owner(core, number, text)
        at = stamp
    core.clock.advance(check_at - at)
    job = core.life_topics.schedule()
    material = json.loads(core.store.one(
        "SELECT e.body_json FROM jobs j JOIN events e USING(event_id) WHERE j.job_id=?",
        (job,))["body_json"])["life_material"] if job else {}
    allowed = ("PRIVATE_MESSAGE_DRAFT" in material.get("allowed_destinations", [])
               and "evt_test_1" in material["source_events"])
    record_property("expected", int(eligible))
    record_property("actual", int(allowed))
    record_property("idle_120s", int(check_at - at >= 120))
    record_property("always_silent", 0)
    if allowed:
        core.model.activity = json.dumps({
            "action": "FOLLOW_UP", "destination": "PRIVATE_MESSAGE_DRAFT",
            "source_event_ids": ["evt_test_1"], "reason": "文件名有可核对的线索", "text": POINT,
        }, ensure_ascii=False)
        assert run(core.run_once("replay", job_id=job))
        assert core.store.one("SELECT text FROM life_activity_results WHERE job_id=?",
                              (job,))["text"] == POINT
    assert allowed is eligible


def test_share_context_uses_actual_dialogue_and_chat_guidance(core):
    core.model = Model()
    core.settings.conversation_policy = ConversationPolicyConfig()
    core.settings.conversation_guidance = "说清自己的具体看法，允许自然收尾。"
    turn(core, 22, "之前说的光谱那一段很有意思", "我也喜欢里面解释恒星颜色的细节。")
    event = ready_share(core, "我想到光谱那一段了。")
    job = core.store.one("SELECT job_id FROM jobs WHERE kind='activity'")["job_id"]
    assert run(core.run_once(job_id=job))
    messages, _ = core.model.calls[0]
    assert core.settings.conversation_guidance in messages[0]["content"]
    assert "光谱那一段很有意思" in messages[1]["content"]
    assert "解释恒星颜色" in messages[1]["content"]
    assert json.loads(messages[-1]["content"])["text"] == event.payload.text
    assert core.store.one("SELECT 1 FROM job_sources WHERE job_id=? AND event_id='evt_test_22'",
                          (job,))


def test_three_internal_choices_leave_contact_capacity_for_a_fourth_item(core):
    setup(core)
    core.settings.proactive_daily_limit = 1
    for number, (source, action, destination) in enumerate([
        ("木星的卫星", "EXPLORE", "NONE"), ("钢琴踏板", "SKIP", "NONE"),
        ("海边的潮汐", "SHARE", "PRIVATE_DIARY"), (ROOT, "FOLLOW_UP", "PRIVATE_MESSAGE_DRAFT"),
    ], 1):
        owner(core, number, source)
        core.clock.advance(121)
        core.model.activity = json.dumps({
            "action": action, "destination": destination, "reason": "只判断本次候选",
            "source_event_ids": [f"evt_test_{number}"],
            "text": "" if destination == "NONE" else source,
        }, ensure_ascii=False)
        job = core.life_topics.schedule()
        assert job and run(core.run_once("replay", job_id=job))
        assert core.store.one("SELECT action FROM life_activity_results WHERE job_id=?",
                              (job,))["action"] == action
    assert len(core.model.calls) == 4
    assert proactive_counts(core.store, core.settings, core.clock()) == (0, False)


def test_busy_allows_private_work_and_global_stop_uses_original_owner_control(core):
    setup(core)
    owner(core, 1, ROOT)
    owner(core, 2, BUSY)
    core.clock.advance(121)
    job = core.life_topics.schedule()
    assert job
    core.model.activity = json.dumps({
        "action": "SHARE", "destination": "PRIVATE_DIARY", "reason": "先记下来",
        "source_event_ids": ["evt_test_1"], "text": "下次再想想文件名的影响。",
    }, ensure_ascii=False)
    assert run(core.run_once(job_id=job))
    assert core.store.one("SELECT state FROM life_activity_results")["state"] == "SHADOW"
    assert feedback_paused(core.store, core.settings, core.clock())
    assert not core.store.rows("SELECT 1 FROM outbox")
    ingest(core, 3, "/暂停自主活动")
    assert run(core.run_once("owner-command"))
    assert core.store.one("SELECT value FROM runtime_meta WHERE key='life_paused'")["value"] == "1"


def test_topic_resume_keeps_separate_contact_pause(core):
    setup(core)
    owner(core, 1, ROOT)
    owner(core, 2, "先别聊这个")
    assert feedback_paused(core.store, core.settings, core.clock(), ["evt_test_1"])
    assert not feedback_paused(core.store, core.settings, core.clock())
    owner(core, 3, "继续这个话题")
    assert not feedback_paused(core.store, core.settings, core.clock(), ["evt_test_1"])
    owner(core, 4, "今天别主动找我")
    owner(core, 5, "继续这个话题")
    assert feedback_paused(core.store, core.settings, core.clock(), ["evt_test_1"])
    assert feedback_paused(core.store, core.settings, core.clock())


def test_similar_framing_keeps_distinct_subjects(core):
    setup(core)
    owner(core, 1, "导出的问题还没解决，明天继续")
    owner(core, 2, "早餐的问题还没解决，明天继续")
    assert core.store.one("SELECT COUNT(*) n FROM life_topics WHERE state='WAITING'")["n"] == 2
