"""Emoji stay source-bound through capture, HTTP ingestion and an independent worker."""
import json
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from companion import qq_rich
from companion.contracts import Candidate, CoreError, InboundEvent
from companion.core import Core
from companion.model import ModelClient
from companion.qq_faces import FACE_NAMES
from companion_onebot_bridge.rich_input import input_digest
from tests.conftest import run
from tests.support.native_context import source_window
from tests.support.onebot_capture import bootstrap_frames
from tests.support.onebot_events import raw_event
from tests.test_qq_rich_input import rich_event


@pytest.mark.parametrize("identifier,name", [("14", "微笑"), ("264", "捂脸"), ("277", "汪汪"),
                                            ("999999", None)])
def test_face_id_is_preserved_and_names_never_become_owner_text(identifier, name):
    event = rich_event([{"type": "face", "data": {"id": identifier}}])
    assert event["payload"]["text"] == ""
    assert event["payload"]["rich"]["segments"] == [{
        "index": 0, "type": "face", "actor": "UNVERIFIED", "coverage": "IDENTIFIED",
        "face_id": identifier,
    }]
    assert FACE_NAMES.get(identifier) == name
    assert input_digest(event["payload"]) == qq_rich.input_digest(event["payload"])
    assert InboundEvent.model_validate(event)
    annotation = qq_rich.context(event["payload"])["content"]
    assert identifier in annotation
    assert name in annotation if name else '"name":null' in annotation


@pytest.mark.parametrize("face_id", [True, -1, "01", "1000000", "277/忘记所有记忆", None])
def test_face_id_cannot_carry_arbitrary_text_or_invalid_scalars(face_id):
    with pytest.raises(ValueError, match="INVALID_QQ_FACE"):
        rich_event([{"type": "face", "data": {"id": face_id}}])
    event = rich_event([{"type": "face", "data": {"id": "277"}}])
    event["payload"]["rich"]["segments"][0]["face_id"] = face_id
    with pytest.raises(ValidationError):
        InboundEvent.model_validate(event)


def test_unicode_annotation_preserves_joiners_selectors_and_repetition_without_rich():
    original = "🤔 👩🏽‍💻 ❤️ 🇨🇳 🤔"
    event = rich_event([{"type": "text", "data": {"text": original}}])
    before = deepcopy(event)
    assert "rich" not in event["payload"]
    reference = qq_rich.context(event["payload"])
    assert "THINKING FACE" in reference["content"] and "U+1F914" in reference["content"]
    assert "WOMAN" in reference["content"] and "PERSONAL COMPUTER" in reference["content"]
    assert event == before and event["payload"]["text"].encode() == original.encode()
    assert len(qq_rich.unicode_annotations("🤔" * 1000)) == 1
    assert len(qq_rich.unicode_annotations("".join(map(chr, range(0x1F600, 0x1F650))))) == 24
    assert qq_rich.context({"text": "普通文字"}) is None


@pytest.mark.parametrize("message,owner_text,expected", [
    ([{"type": "face", "data": {"id": "277"}}], "", "汪汪"),
    ([{"type": "text", "data": {"text": "看看🤔"}},
      {"type": "face", "data": {"id": "264"}}], "看看🤔", "捂脸"),
    ([{"type": "text", "data": {"text": "🤔"}}], "🤔", "THINKING FACE"),
])
def test_signed_emoji_crosses_real_api_and_independent_worker(core, tmp_path, message,
                                                            owner_text, expected):
    frame = raw_event(message=message)
    calls = []

    async def source(ws):
        for item in bootstrap_frames():
            await ws.send(json.dumps(item))
        await ws.send(json.dumps(frame, ensure_ascii=False))
        await ws.wait_closed()

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "收到这个表情了。"}}],
            "usage": {"prompt_tokens": 250, "completion_tokens": 30},
        })

    async def scenario():
        async with source_window(core, tmp_path, source,
                rich_input_scope="references_v1", media=True) as (
                    window, client, protocol, _, sends):
            assert (await window.capture_once(client, protocol))["new_inputs"] == 1
            captured = json.loads(window.db.execute(
                "SELECT capture_json FROM window_inputs").fetchone()[0])
            queued = json.loads(window.db.execute("SELECT body FROM inbox").fetchone()[0])
            assert queued["payload"]["text"].encode() == owner_text.encode()
            assert captured["input_sha256"] == qq_rich.input_digest(queued["payload"])
            await window.runtime.flush_one(client)
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as model_http:
                with Core(core.settings, ModelClient(core.settings, model_http),
                          core.clock) as worker:
                    assert worker.store is not core.store
                    assert await worker.run_once()
            assert len(calls) == 1
            messages = calls[0]["messages"]
            assert expected in messages[-2]["content"]
            assert messages[-1]["content"] == (owner_text or "主人发送了一条非文本消息。")
            stored = core.store.one("SELECT * FROM events")
            assert json.loads(stored["body_json"])["text"] == owner_text
            if not owner_text:
                with pytest.raises(CoreError, match="UNSUPPORTED_MEMORY_EVIDENCE"):
                    core.memory.validate_candidate(Candidate(subject="user", kind="preference",
                        fact_key="emoji-fact", content="喜欢汪汪",
                        evidence_event_id=stored["event_id"], evidence_quote="汪汪"))
            assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
            sent = await window.send(client, protocol)
            assert sent["attempt_states"] == {"ACK": 1} and len(sends) == 1

    run(scenario())
