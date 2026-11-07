"""Chronological, synthetic replay. The provider is fixed; labels measure eligibility."""

import json

import pytest

from companion.conversation_policy import ConversationPolicyConfig
from companion.life_protocol import LifeLoopConfig
from companion.social import feedback_paused, proactive_counts
from tests.conftest import ingest, run
from tests.support.turn_taking import Model, turn
from tests.test_notices import ready_share

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


@pytest.mark.parametrize(("first", "second"), [
    ("我昨天买了个键盘", "我昨天看了场电影"),
    ("昨天买的键盘失灵了", "昨天买的牛奶过期了"),
])
def test_shared_time_and_sentence_frame_do_not_postpone_topic(core, first, second):
    setup(core)
    owner(core, 1, first)
    old = core.store.one("SELECT * FROM life_topics")
    core.clock.advance(60)
    owner(core, 2, second)
    assert core.store.one("SELECT * FROM life_topics WHERE topic_id=?", (old["topic_id"],)) == old
    assert core.store.one("SELECT COUNT(*) n FROM life_topics")["n"] == 2


def test_original_subject_survives_deictic_addendum(core):
    setup(core)
    owner(core, 1, "我昨天买了个键盘")
    owner(core, 2, "补充一下，是蓝色的")
    owner(core, 3, "那个键盘按起来挺轻快")
    assert core.store.one("SELECT COUNT(*) n FROM life_topics")["n"] == 1
    assert len(core.store.rows("SELECT event_id FROM life_topic_sources")) == 3


def test_two_settled_preemptions_park_until_related_input(core):
    setup(core)
    owner(core, 1, ROOT)
    topic = core.store.one("SELECT * FROM life_topics")["topic_id"]
    core.clock.advance(120)
    for index, unrelated in enumerate([BREAKFAST, "深夜听了爵士乐"], 2):
        job = core.life_topics.schedule()
        assert core.store.one("SELECT topic_id FROM life_activity_results WHERE job_id=?",
                              (job,))["topic_id"] == topic
        core.model.callback = lambda i=index, msg=unrelated: ingest(core, i, msg)
        assert run(core.run_once("replay", job_id=job))
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE kind='chat'")
        core.life_topics.synchronize()
        core.clock.advance(61)
    state = core.store.one("SELECT state,reason_code FROM life_topics WHERE topic_id=?", (topic,))
    assert state == {"state": "WAITING", "reason_code": "WAITING_FOR_NEW_EVIDENCE"}
    outcomes = core.store.rows("SELECT state FROM life_activity_results WHERE topic_id=?", (topic,))
    assert outcomes == [{"state": "INVALIDATED"}] * 2
    assert core.store.rows("SELECT state FROM model_usage") == [{"state": "SETTLED"}] * 2
    owner(core, 4, "导出文件的问题有个新线索")
    assert core.store.one("SELECT decision_count FROM life_topics WHERE topic_id=?", (topic,)) == {
        "decision_count": 1}
    core.clock.advance(120)
    core.life_topics.synchronize()
    assert core.life_topics.material(topic)["source_events"].keys() == {"evt_test_1", "evt_test_4"}


@pytest.mark.parametrize(("text", "paused", "later"), [
    ("我去上课了，晚点再聊", True, False), ("今天别主动找我了", True, True),
    ("我不是说今天别主动找我了", False, False), ("她说我去上课了，晚点再聊", False, False),
    ("“今天别主动找我了”是什么意思？", False, False),
])
def test_contact_paraphrases_keep_scope_and_expiration(core, text, paused, later):
    setup(core)
    owner(core, 1, text)
    assert feedback_paused(core.store, core.settings, core.clock()) is paused
    core.clock.advance(4 * 3600)
    assert feedback_paused(core.store, core.settings, core.clock()) is later
    owner(core, 2, "我回来了，继续聊")
    assert not feedback_paused(core.store, core.settings, core.clock())


@pytest.mark.parametrize(("first", "second"), [
    ("电脑打不开了", "门打不开了"),
    ("面试结束了，还在等结果", "体检结束了，还在等结果"),
])
def test_shared_predicate_without_shared_subject_keeps_topics_apart(core, first, second):
    setup(core)
    owner(core, 1, first)
    core.clock.advance(60)
    owner(core, 2, second)
    assert core.store.one("SELECT COUNT(*) n FROM life_topics")["n"] == 2


@pytest.mark.parametrize("update", ["那台打印机又连不上了", "搞定啦，多谢"])
def test_same_subject_or_resolution_reply_updates_the_previous_topic(core, update):
    setup(core)
    owner(core, 1, "打印机连不上WiFi")
    core.clock.advance(60)
    owner(core, 2, update)
    assert core.store.one("SELECT COUNT(*) n FROM life_topics")["n"] == 1
    assert len(core.store.rows("SELECT event_id FROM life_topic_sources")) == 2
