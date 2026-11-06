"""New shares select owner-grounded history without resetting ordinary dialogue."""

import pytest

from companion import context_selection as selection
from companion.contracts import Candidate, InboundEvent
from companion.store import encode
from tests.conftest import event, run
from tests.support import native_image_memory as image_fixture
from tests.test_dialogue_context import current_job, sent_history
from tests.test_image_review import reply


def test_media_only_input_does_not_inherit_the_previous_text_topic():
    sources = [{"user_text": "旧的采购计划", "reply_text": "我猜你要发图提醒采购。"}]
    assert selection.focus("", sources, shared=True) == set()
    assert selection.focus("", sources) == selection.terms("旧的采购计划")
    assert selection.focus("继续", sources) == selection.terms("旧的采购计划")


def test_share_history_matches_owner_words_not_assistant_guesses_and_keeps_pending():
    sources = [
        {"event_id": "old", "user_text": "旧的采购计划", "reply_text": "你要发宇航服吧"},
        {"event_id": "related", "user_text": "宇航服照片", "reply_text": "看到了"},
        {"event_id": "pending", "user_text": "还有一个问题没答", "reply_text": "",
         "reply_complete": False},
    ]
    assert [s["event_id"] for s in selection.share_history(
        sources, selection.terms("宇航服"))] == ["related", "pending"]
    assert len(sources) == 3


@pytest.mark.parametrize(("old", "current"), [
    ("我们一起研究采购预算", "我们一起看看这张"),
    ("我想学习钢琴", "我想喝杯咖啡"),
    ("look at this picture of my keyboard", "look at this picture of a rocket"),
    ("[10:00] 刚才游戏输了，我有点不服", "[21:00] 看下这张"),
])
def test_shared_function_words_do_not_reintroduce_an_unrelated_topic(old, current):
    source = {"user_text": old, "reply_text": "我猜你会发图续上这件事"}
    assert selection.share_history([source], selection.terms(current)) == []


def test_current_share_focus_cannot_inherit_an_older_observation():
    old = {"owner_event_id": "old", "text": "旧的采购计划"}
    current = {"owner_event_id": "current", "text": "宇航服照片", "trust": "quoted_data"}
    reference, topic = selection.current_share(encode({"shared_content": [old, current]}),
                                                "current")
    assert "旧的采购计划" not in reference
    assert "宇航服" in reference and topic == selection.terms("宇航服照片")
    assert selection.current_share(encode({"shared_content": [old]}), "current") == ("", set())


@pytest.mark.parametrize("text", ["就是刚才说的", "再看这张，角色换了", "这个呢", "继续",
                                "结合我以前的需求", "same as before"])
def test_explicit_share_continuations_keep_their_history(text):
    assert selection.share_continuation(text)


@pytest.mark.parametrize("text", ["", "看看这张", "哈哈", "换个话题", "这张和刚才无关",
                                "这张和刚才没有关系", "unrelated to before"])
def test_fresh_shares_do_not_become_continuations(text):
    assert not selection.share_continuation(text)


def test_real_core_assembly_filters_a_new_share_without_erasing_the_old_turn(core):
    sent_history(core, 1, "旧的采购计划", "我猜你要发图片提醒采购")
    data = event(core, 99, "").model_dump()
    data.update(event_type="message.media_received", payload={
        "text": "", "media": [],
        "shared_content": [{"kind": "link", "url": "https://example.org/new-share"}],
    })
    shared = InboundEvent.model_validate(data)
    core.ingest(shared, "new-share")
    job = core.store.one("SELECT j.*,e.body_json,e.conversation_id FROM jobs j JOIN events e "
                         "ON e.event_id=j.event_id WHERE j.event_id='evt_test_99'")
    messages = core.context(job)
    assert all("旧的采购计划" not in str(m["content"]) for m in messages)
    assert core.store.one("SELECT 1 FROM events WHERE event_id='evt_test_1'")
    assert core.store.one("SELECT state FROM outbox WHERE origin_event_id='evt_test_1'")[
        "state"] == "SENT"


def test_text_only_small_talk_retains_familiar_history(core):
    sent_history(core, 1, "旧的采购计划", "我猜你要发图片提醒采购")
    messages = core.context(current_job(core, text="继续"))
    assert any("旧的采购计划" in str(m["content"]) for m in messages)


def test_native_new_picture_keeps_relationship_but_not_an_unrelated_sent_guess(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with image_fixture.native_image_memory(core, tmp_path, history=True) as context:
            assert await context.worker.run_once()
            await context.entry.send(context.entry.client, context.entry.protocol)
            owner_text = "你是我女儿，我是爸爸。另一个话题：我们一起研究采购预算。"
            old = await context.entry.admit(2, owner_text)
            await reply(core, context.entry, "我猜你晚点还会发图提醒采购预算。", [])
            with core.store.transaction():
                core.memory.add(Candidate(subject="relationship", kind="fact",
                    fact_key="family", content="你是我女儿，我是爸爸。",
                    evidence_event_id=old["event_id"], evidence_quote="你是我女儿，我是爸爸。"),
                    core.clock(), confirmed=True)
            monkeypatch.setattr(image_fixture, "QUESTION", "我们一起看看这张")
            next_job = await context.admit_image(3)
            start = len(context.calls)
            assert await context.worker.run_once()
            await context.entry.send(context.entry.client, context.entry.protocol)
            calls = context.calls[start:]
            assert calls
            for call in calls:
                payload = encode(call["messages"])
                assert "采购预算" not in payload
                assert "你是我女儿，我是爸爸" in payload
                assert "你们的关系" in payload
            assert core.store.one("SELECT state FROM outbox WHERE origin_event_id=?",
                                  (old["event_id"],))["state"] == "SENT"
            assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                                  (next_job["job_id"],))["state"] == "SUCCEEDED"

    run(scenario())
