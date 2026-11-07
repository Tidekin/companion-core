"""Only an attributed image objection may quarantine earlier image evidence."""

import json

import pytest

from companion import image_review, memory_references, reply_quote
from companion.contracts import CoreError
from tests.conftest import run
from tests.support.native_image_memory import native_image_memory
from tests.support.onebot_reference_reader import reply as quote_segment
from tests.support.onebot_reference_reader import text as text_segment
from tests.test_image_review import OBSERVATION, reply


@pytest.mark.parametrize("text,linked,immediate,wanted,objection", [
    ("错了错了", False, True, True, False),
    ("错了错了", True, True, True, False),
    ("我错了，我不养其她鱼", False, True, False, False),
    ("我没说你看错了", False, True, False, False),
    ("我没有说你理解错了", True, True, False, False),
    ("不是说你看错了", True, True, False, False),
    ("他说‘你看错了’", False, True, False, False),
    ("他说，你看错了。你理解错了。", True, True, False, False),
    ("他说：你看错了。你理解错了。", True, True, False, False),
    ("我没说你看错了，你理解错了", True, True, False, False),
    ("‘你看错了。你理解错了。’", False, True, False, False),
    ("他说‘你看错了。你理解错了。", True, True, False, False),
    ("他说‘你看错了’，但我认为你对这张图的解读不对", False, True, True, True),
    ("你没有看错", False, True, False, False),
    ("你看错了，不过我没说你看错了", True, True, True, False),
    ("你看错了，你没有看错", True, True, True, False),
    ("‘我没说你看错了’，你这张图理解错了", False, True, True, True),
    ("你没看错", True, True, False, False),
    ("你看错了", False, True, True, True),
    ("你看错了", False, False, True, False),
    ("你看错了", True, False, True, True),
    ("你看错了？", True, True, True, False),
    ("你看错了吧", True, True, True, False),
    ("你理解错了", False, True, True, False),
    ("你理解错了", True, False, True, True),
    ("你理解错了，我不是嫌弃你", False, True, True, False),
    ("你理解错了，我不是嫌弃你。他说，两张图的动作和台词一样。",
     False, True, True, False),
    ("你理解错了，两张图的动作和台词一样？", False, True, True, False),
    ("你理解错了，两张图的动作和台词一样", False, False, True, True),
    ("你把这张图看反了", False, False, True, True),
    ("你这张图看错了", False, False, True, True),
    ("这张图你理解错了", False, False, True, True),
    ("这张图的解读不对", False, False, True, True),
    ("你对这张图的理解有偏差", False, False, True, True),
    ("你刚才的解释不对", False, True, True, False),
    ("你刚才的解释不对", True, True, True, True),
    ("不对", False, True, True, False),
    ("不对", True, False, True, True),
    ("不是这样", True, False, True, True),
    ("重新看这张图", False, True, True, False),
    ("两张图的动作和台词一样", False, True, True, False),
])
def test_only_own_image_objections_are_persistent(text, linked, immediate, wanted, objection):
    assert image_review._intent(text, linked=linked, immediate=immediate) == (wanted, objection)


@pytest.mark.parametrize("text", [
    "错了错了", "我错了，我不养其她鱼", "我没说你看错了", "他说‘你看错了’",
    "你理解错了，他说‘{source}’",
])
def test_other_meanings_of_wrong_do_not_quarantine_image_evidence(core, tmp_path, text):
    async def scenario():
        fixture = native_image_memory(core, tmp_path, history=True, record_visible=True,
                                      reply="下图推翻了上图。", core_options=OBSERVATION)
        async with fixture as context:
            assert await context.worker.run_once()
            await context.entry.send(context.entry.client, context.entry.protocol)
            source = core.store.one("SELECT * FROM events WHERE event_id=?",
                                    (context.job["event_id"],))
            reference = next(value for value in memory_references.inputs(core, source)
                             if value["kind"] == "image")
            original = memory_references.read(core.memory, reference["reference_id"], core.clock())
            await context.entry.admit(2, text.format(source=source["event_id"]))
            calls = []
            await reply(core, context.entry, "先核对你这句话的意思。", calls)
            assert memory_references.image_reviews(core.store, source["event_id"]) == []
            assert memory_references.read(core.memory, reference["reference_id"],
                                          core.clock()) == original
            if text == "错了错了":
                payload = json.dumps(calls[-1], ensure_ascii=False)
                assert image_review.HEADING.strip() in payload
                assert '"owner_objection":false' in payload.replace('\\"', '"')
            if text == "我错了，我不养其她鱼":
                await context.entry.admit(3, "你看错了")
                await reply(core, context.entry, "你指刚才哪一处理解？", calls)
                assert memory_references.image_reviews(core.store, source["event_id"]) == []
            await context.entry.admit(4, "接着聊刚才的图片")
            await reply(core, context.entry, "从图中实际可见内容继续聊。", calls)
            replay = json.dumps(calls[-1], ensure_ascii=False)
            assert "座位B17" in replay
            assert "本轮未带齐关联的纠正来源" not in replay
            assert memory_references.read(core.memory, reference["reference_id"],
                                          core.clock()) == original
    run(scenario())


@pytest.mark.parametrize("text,valid_quote,objection", [
    ("不对", True, True), ("你理解错了", True, True),
    ("我错了", True, False), ("不是这样", False, False),
    ("你看错了", False, False),
])
def test_native_quote_links_target_without_granting_objection(
    core, tmp_path, text, valid_quote, objection,
):
    async def scenario():
        old_reply = "下图推翻了上图。"
        fixture = native_image_memory(core, tmp_path, history=True, record_visible=True,
                                      reply=old_reply, core_options=OBSERVATION)
        async with fixture as context:
            assert await context.worker.run_once()
            await context.entry.send(context.entry.client, context.entry.protocol)
            source = core.store.one("SELECT * FROM events WHERE event_id=?",
                                    (context.job["event_id"],))
            reference = next(value for value in memory_references.inputs(core, source)
                             if value["kind"] == "image")
            posted = await context.entry.admit(2, message=[
                quote_segment("123456", message=[text_segment(
                    old_reply if valid_quote else "与本地已发记录不符的引用")]),
                text_segment(text),
            ])
            job = core.store.one("SELECT j.*,e.conversation_id FROM jobs j "
                "JOIN events e USING(event_id) WHERE j.event_id=?", (posted["event_id"],))
            body = json.loads(core.store.one("SELECT body_json FROM events WHERE event_id=?",
                                              (posted["event_id"],))["body_json"])
            quotes = reply_quote.self_quote_indices(core.store, core.settings, job, body)
            assert bool(quotes) == valid_quote
            calls = []
            await reply(core, context.entry, "对照具体来源核对理解。", calls)
            reviews = memory_references.image_reviews(core.store, source["event_id"])
            assert len(reviews) == int(objection)
            if objection:
                with pytest.raises(CoreError, match="MEMORY_REFERENCE_CHANGED"):
                    memory_references.read(core.memory, reference["reference_id"], core.clock())
            else:
                assert memory_references.read(core.memory, reference["reference_id"], core.clock())
    run(scenario())
