"""Native QQ quotes: the model names a shown alias; Core resolves its own owner input."""

import json

import httpx
import pytest

from companion import reply_quote
from companion.model import ModelClient
from companion.reply_segments import SegmentedReplyConfig
from tests.conftest import run
from tests.support.native_context import source_window
from tests.support.onebot_capture import bootstrap_frames
from tests.support.onebot_events import raw_event


def test_choice_marker_is_parsed_once_and_always_removed():
    assert reply_quote.extract("【引用 m1a2b】是那次吧") == ("m1a2b", "是那次吧")
    assert reply_quote.extract("好呀【引用 m1a2b】【引用 m3c4d】") == ("m1a2b", "好呀")
    assert reply_quote.extract("没有引用") == (None, "没有引用")
    assert reply_quote.alias("evt_" + "0" * 64) == reply_quote.alias("evt_" + "0" * 64)


@pytest.mark.parametrize("label", [
    "[编号 m1a2b]", "【编号 m1a2b】", "[编号 m...]", "【编号 m...】",
    "[编号 m…]", "【编号 m……】",
])
def test_internal_labels_and_template_placeholders_are_not_visible_text(label):
    assert reply_quote.extract(label + " 我记得。\n" + label + "后来呢？") == (
        None, "我记得。\n后来呢？")


@pytest.mark.parametrize("text", ["编号 m 是这款设备的型号", "[编号 1234]", "【编号 m12345】"])
def test_ordinary_numbered_content_is_preserved(text):
    assert reply_quote.extract(text) == (None, text)


def first_event(core):
    return core.store.one(
        "SELECT event_id FROM events WHERE source='qq.onebot' AND "
        "actor_id='owner' ORDER BY record_sequence LIMIT 1"
    )["event_id"]


def converse(core, tmp_path, second_reply, *, policy="occasionally"):
    """Two real owner turns through capture, Core and the checked Bridge send path."""
    number, requests = 1, []

    async def source(ws):
        for frame in bootstrap_frames():
            await ws.send(json.dumps(frame))
        await ws.send(json.dumps(raw_event(number, text=["我今天去爬山了", "累死了"][number - 1])))
        await ws.wait_closed()

    def respond(request):
        requests.append(json.loads(request.content))
        text = "听起来很棒" if len(requests) == 1 else second_reply(core)
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    async def scenario():
        nonlocal number
        options = {"max_history_utf8_bytes": 4000, "core_options": {
            "segmented_reply": SegmentedReplyConfig(min_chars=100, interval_ms=0),
            "quote_policy": policy}}
        async with source_window(core, tmp_path, source, scope_options=options) as values:
            captured, client, protocol, _, sends = values
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as model:
                core.model = ModelClient(core.settings, model)
                for number in (1, 2):
                    assert (await captured.capture_once(client, protocol))["new_inputs"] == 1
                    assert await captured.runtime.flush_one(client)
                    assert await core.run_once("quote-turn")
                    assert (await captured.send(client, protocol))["attempt_states"] == {
                        "ACK": number}
            return [body for body in sends if "message" in body], requests

    return run(scenario())


def reply_parts(core):
    return [json.loads(row["content_json"]) for row in core.store.rows(
        'SELECT content_json FROM outbox ORDER BY record_sequence')]


def test_named_older_owner_message_is_sent_as_native_quote(core, tmp_path):
    def older(core):
        return "【引用 " + reply_quote.alias(first_event(core)) + "】那座山高吗"

    sends, requests = converse(core, tmp_path, older)
    alias = reply_quote.alias(first_event(core))
    shown = json.dumps(requests[1], ensure_ascii=False)
    assert "[编号 " + alias + "] 我今天去爬山了" in shown and "【引用 编号】" in shown
    assert reply_parts(core)[1] == {"text": "那座山高吗", "reply_to": {"message_id": "1"}}
    assert sends[1]["message"] == [{"type": "reply", "data": {"id": "1"}},
                                   {"type": "text", "data": {"text": "那座山高吗"}}]
    assert sends[0]["message"] == [{"type": "text", "data": {"text": "听起来很棒"}}]


@pytest.mark.parametrize("choice,policy", [
    (lambda core: "【引用 m0000】那座山高吗", "occasionally"),
    (lambda core: "【引用 " + reply_quote.alias(core.store.one(

            "SELECT event_id FROM events WHERE source='qq.onebot' AND "
            "actor_id='owner' ORDER BY record_sequence DESC LIMIT 1"
        )["event_id"]) + "】那座山高吗", "occasionally"),
])
def test_unknown_or_latest_choice_sends_plain_text_without_marker(core, tmp_path, choice,
                                                                   policy):
    sends, _ = converse(core, tmp_path, choice, policy=policy)
    assert reply_parts(core)[1] == {"text": "那座山高吗"}
    assert sends[1]["message"] == [{"type": "text", "data": {"text": "那座山高吗"}}]


def test_never_policy_shows_no_aliases(core, tmp_path):
    _, requests = converse(core, tmp_path, lambda core: "那座山高吗", policy="never")
    shown = json.dumps(requests[1], ensure_ascii=False)
    assert "[编号 " not in shown and "【引用" not in shown
    assert reply_parts(core)[1] == {"text": "那座山高吗"}


@pytest.mark.parametrize("removal", ["erased", "recalled"])
def test_erased_or_recalled_target_is_never_resolved(core, tmp_path, removal):
    from companion import input_recall

    converse(core, tmp_path, lambda core: "那座山高吗")
    first = core.store.one('SELECT * FROM events WHERE event_id=%s', (first_event(core),))
    job = core.store.one(
        'SELECT j.*,e.conversation_id FROM jobs j JOIN events e '
        "USING(event_id) WHERE e.source='qq.onebot' AND e.actor_id='owner' "
        'ORDER BY e.record_sequence DESC'
    )
    choice = reply_quote.alias(first["event_id"])

    def resolve():
        with core.store.transaction():
            return reply_quote.target(core.store, core.settings, core.clock, job, choice)

    assert resolve() == "1"
    key, value = (("erased_event:" + first["event_id"], "{}") if removal == "erased" else
                  (input_recall.marker_key(first["source_event_id"]),
                   json.dumps({"state": "RECALLED"})))
    core.store.execute('INSERT INTO runtime_meta(key,value) VALUES(%s,%s)', (key, value))
    assert resolve() is None


def test_reply_to_coalesced_inputs_quotes_the_latest_input(core, tmp_path):
    number = 1

    async def source(ws):
        for frame in bootstrap_frames():
            await ws.send(json.dumps(frame))
        await ws.send(json.dumps(raw_event(number, text=["在吗", "我今天去爬山了"][number - 1])))
        await ws.wait_closed()

    def respond(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "在呢，爬山累吗"}}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    async def scenario():
        nonlocal number
        options = {"max_history_utf8_bytes": 4000, "core_options": {
            "segmented_reply": SegmentedReplyConfig(min_chars=100, interval_ms=0),
            "quote_policy": "occasionally"}}
        async with source_window(core, tmp_path, source, scope_options=options) as values:
            captured, client, protocol, _, sends = values
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as model:
                core.model = ModelClient(core.settings, model)
                for value in (1, 2):
                    number = value
                    assert (await captured.capture_once(client, protocol))["new_inputs"] == 1
                    assert await captured.runtime.flush_one(client)
                while await core.run_once("quote-coalesced"):
                    pass
                assert (await captured.send(client, protocol))["attempt_states"] == {"ACK": 1}
            return [body for body in sends if "message" in body]

    sends = run(scenario())
    assert reply_parts(core) == [{"text": "在呢，爬山累吗", "reply_to": {"message_id": "2"}}]
    assert sends[0]["message"][0] == {"type": "reply", "data": {"id": "2"}}


@pytest.mark.parametrize("change", ["no_authority", "later_part", "voice", "proactive",
                                    "bad_id", "extra_key"])
def test_bridge_refuses_quotes_outside_a_first_native_reply(change):
    from companion_onebot_bridge import reply_segments

    item = {"authority": {}, "action_type": "qq.private.reply",
            "content": {"text": "好", "reply_to": {"message_id": "7"}}}
    if change == "no_authority":
        item.pop("authority")
    elif change == "later_part":
        item["reply_segment"] = {"index": 1}
    elif change == "voice":
        item["voice"] = {}
    elif change == "proactive":
        item["action_type"] = "qq.private.proactive"
    elif change == "bad_id":
        item["content"]["reply_to"] = {"message_id": "07"}
    else:
        item["content"]["reply_to"] = {"message_id": "7", "text": "x"}
    with pytest.raises(ValueError, match="QUOTE_SCOPE_REQUIRED"):
        reply_segments.quote(item)
    item = {"authority": {}, "action_type": "qq.private.reply", "reply_segment": {"index": 0},
            "content": {"text": "好", "reply_to": {"message_id": "-7"}}}
    assert reply_segments.quote(item) == [{"type": "reply", "data": {"id": "-7"}}]


def first_reply(core):
    return core.store.one(
        'SELECT message_id FROM outbox ORDER BY record_sequence LIMIT 1'
    )["message_id"]


@pytest.mark.parametrize("label", ["[编号 m0000]", "【编号 m1a2b】", "[编号 m...]"])
def test_named_own_earlier_reply_is_quoted_by_its_recorded_provider_id(core, tmp_path, label):
    def own(core):
        return "【引用 " + reply_quote.alias(first_reply(core)) + "】" + label + " 我是认真的"

    sends, requests = converse(core, tmp_path, own)
    shown = json.dumps(requests[1], ensure_ascii=False)
    assert "[编号 " + reply_quote.alias(first_reply(core)) + "] 听起来很棒" in shown
    assert reply_parts(core)[1] == {"text": "我是认真的", "reply_to": {"message_id": "123456"}}
    assert sends[1]["message"] == [{"type": "reply", "data": {"id": "123456"}},
                                   {"type": "text", "data": {"text": "我是认真的"}}]


def test_erased_own_reply_is_never_resolved(core, tmp_path):
    converse(core, tmp_path, lambda core: "那座山高吗")
    job = core.store.one(
        'SELECT j.*,e.conversation_id FROM jobs j JOIN events e '
        "USING(event_id) WHERE e.source='qq.onebot' AND e.actor_id='owner' "
        'ORDER BY e.record_sequence DESC'
    )
    choice = reply_quote.alias(first_reply(core))

    def resolve():
        with core.store.transaction():
            return reply_quote.target(core.store, core.settings, core.clock, job, choice)

    assert resolve() == "123456"
    core.store.execute("UPDATE outbox SET content_json='{}',content_sha256='' WHERE message_id=%s",
                       (first_reply(core),))
    assert resolve() is None
