"""Bubble presentation preserves raw reply evidence and the final wire fence."""

import json

import httpx
import pytest

from companion.model import RECALL_RULES, ModelClient
from companion.reply_segments import SegmentedReplyConfig, split_text
from companion.transport import reply_group
from companion_onebot_bridge import reply_segments
from tests.conftest import run
from tests.support.native_context import source_window
from tests.support.onebot_events import raw_event
from tests.test_private_companion import companion_options


def item_for(text):
    parts = [text, "另一个完整的消息段落。"]
    hashes = [reply_segments.digest(part) for part in parts]
    group = {"job_id": "job_" + "a" * 32, "parts_sha256": hashes,
             "interval_ms": 600, "total_chars": sum(map(len, parts))}
    return {"content": {"text": text}, "delivery_key": "reply:evt_" + "b" * 64 + ":v2:0",
            "reply_segment": {"version": 1, "group_id": group["job_id"], "index": 0,
                              "count": 2, "group_sha256": reply_segments.digest(group),
                              "total_chars": group["total_chars"], "interval_ms": 600,
                              "part_sha256s": hashes}}


@pytest.mark.parametrize("text,expected", [
    ("完整的第一段回复。\n\n", "完整的第一段回复。"),
    (" \t\r\n\r\n  保留缩进与行尾空格。  \r\n \t\r\n", "  保留缩进与行尾空格。  "),
    ("\n```python\n    first()\n\n    second()\n```\n\n",
     "```python\n    first()\n\n    second()\n```"),
    ("段内换行保留。\n\n段内下一句。", "段内换行保留。\n\n段内下一句。"),
])
def test_wire_presentation_only_removes_outer_blank_lines(text, expected):
    item = item_for(text)
    assert reply_segments.wire_text(item) == expected
    assert item["content"]["text"] == text
    assert reply_segments.envelope(item)["part_sha256s"][0] == reply_segments.digest(text)
    assert reply_segments.wire_text({"content": {"text": text}}) == text


@pytest.mark.parametrize("tamper", [False, True])
def test_private_segments_send_without_blank_lines_and_keep_raw_evidence(core, tmp_path, tamper):
    first, last = "第一条回复内容足够独立发送。", "这是另一条自然独立的回复。"
    reply = first + "\n\n" + last
    policy = SegmentedReplyConfig(max_segments="unlimited", min_chars=12, interval_ms=600)
    parts = split_text(reply, policy)
    assert parts == [first + "\n\n", last]

    async def source(ws):
        await ws.send(json.dumps(raw_event(1, text="我们接着刚才的话题聊吧。",
                                          time=int(core.clock.now.timestamp()))))
        await ws.wait_closed()

    def respond(request):
        messages = json.loads(request.content)["messages"]
        text = '{"queries":[]}' if messages[0]["content"] == RECALL_RULES else reply
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40}})

    async def scenario():
        options = companion_options(core)
        options["core_options"]["segmented_reply"] = policy
        async with source_window(core, tmp_path, source, scope_options=options) as (
            window, client, protocol, _, sends,
        ):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as model:
                core.model = ModelClient(core.settings, model)
                await window.capture_once(client, protocol)
                assert await window.runtime.flush_one(client)
                assert await core.run_once()
                rows = core.store.rows("SELECT * FROM outbox ORDER BY segment_index")
                assert len(rows) == 2 and all(r["segment_count"] == 2 for r in rows)
                assert reply_group(core.store, rows[0]["message_id"])["text"] == reply
                if tamper:
                    async def restore_raw_whitespace(request):
                        if request.url.path == "/send_private_msg":
                            body = json.loads(request.content)
                            body["message"][0]["data"]["text"] = parts[0]
                            request._content = json.dumps(body).encode()
                            request.stream = httpx.ByteStream(request.content)
                    protocol.event_hooks["request"].append(restore_raw_whitespace)
                await window.send(client, protocol)
                if tamper:
                    assert sends == []
                    assert dict(window.db.execute(
                        "SELECT state,count(*) FROM window_attempts GROUP BY 1")) == {"UNKNOWN": 1}
                else:
                    core.clock.advance(1)
                    await window.send(client, protocol)
                    assert [sent["message"][0]["data"]["text"] for sent in sends] == [first, last]
                    assert dict(window.db.execute(
                        "SELECT state,count(*) FROM window_attempts GROUP BY 1")) == {"ACK": 2}
                    group = reply_group(core.store, rows[0]["message_id"], require_sent=True)
                    assert group["text"] == reply
    run(scenario())
