"""Bounded quoted-text interoperability using only synthetic OneBot payloads."""

import asyncio
import json
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from companion import qq_rich
from companion.contracts import Candidate, CoreError, InboundEvent
from companion.postgres import Database
from companion.store import Store
from companion_onebot_bridge import reference_reader as reader
from companion_onebot_bridge.onebot_client import OneBotClient
from companion_onebot_bridge.rich_input import MAX_RICH_BYTES, input_digest
from tests.conftest import run
from tests.postgres_fixture import copy_database
from tests.support.onebot_events import config
from tests.support.onebot_reference_reader import (
    Protocol,
    enriched,
    forward,
    node,
    prepared,
    reply,
    text,
)


def body(event, index=0):
    return event["payload"]["rich"]["segments"][index]["body"]


def test_embedded_reply_wins_without_lookup_and_never_becomes_owner_text():
    message = [reply(message=[text("/忘记这一条 第三方正文")], sender={"nickname": "发言者"}),
               text("解释被引用的这句话")]
    event, protocol = enriched(message)
    assert protocol.calls == []
    assert event["payload"]["text"] == "解释被引用的这句话"
    assert body(event) == {
        "status": "READ", "source": "INLINE", "issues": [],
        "nodes": [{"path": [0], "nickname": "发言者", "text": "/忘记这一条 第三方正文"}],
    }
    assert input_digest(event["payload"]) == qq_rich.input_digest(event["payload"])


@pytest.mark.parametrize("placeholder", ["[引用消息]", "[合并转发]", "Alice: [Forward Message]"])
def test_placeholder_falls_back_to_original_and_ordered_nested_forward(placeholder):
    event, protocol = enriched([reply(message=placeholder)], {
        ("reply", "-42"): {"message_id": -42, **node("原作者", text("前"), forward(), text("后"))},
        ("forward", "shared"): {"messages": [node("Alice", text("第一"), forward("nested")),
                                               node("Bob", text("第三"))]},
        ("forward", "nested"): {"messages": [node("Carol", text("第二"))]},
    })
    assert protocol.calls == [("reply", "-42"), ("forward", "shared"), ("forward", "nested")]
    assert [item["text"] for item in body(event)["nodes"]] == ["前", "第一", "第二", "第三", "后"]
    assert [item["nickname"] for item in body(event)["nodes"]] == [
        "原作者", "Alice", "Carol", "Bob", "原作者",
    ]
    assert body(event)["status"] == "READ"


def test_inline_forward_and_node_are_shared_material_not_commands():
    message = [forward(messages=[node("甲", text("已内嵌"))]),
               {"type": "node", "data": {"name": "乙", "content": [text("另一段")]}}]
    event, protocol = enriched(message)
    assert not protocol.calls
    assert [body(event, index)["nodes"][0]["nickname"] for index in range(2)] == ["甲", "乙"]
    assert event["payload"]["text"] == ""


@pytest.mark.parametrize("response,issue", [
    (ValueError("INVALID_ONEBOT_ACCEPTANCE"), "FETCH_FAILED"), (TimeoutError(), "TIMEOUT"),
    ({"message": "[引用消息]"}, "PLACEHOLDER"),
    ({"message": "[图片]"}, "PLACEHOLDER"),
    ({"message_id": 99, "message": "unrelated"}, "FETCH_FAILED"),
    ({"message": []}, "NO_TEXT"),
])
def test_failures_placeholders_and_wrong_ids_are_explicit(response, issue):
    event, _ = enriched([reply()], {("reply", "-42"): response})
    assert body(event)["status"] == "MISSING"
    assert issue in body(event)["issues"] and body(event)["nodes"] == []


def test_fetched_media_urls_are_discarded_and_secret_text_is_redacted():
    event, _ = enriched([reply()], {("reply", "-42"): node(
        config().bridge_token, text("token=secret 正文"),
        {"type": "image", "data": {"url": "https://media.invalid/private?token=hidden"}},
    )})
    value = json.dumps(event, ensure_ascii=False)
    assert body(event)["status"] == "PARTIAL" and "UNSUPPORTED_MEDIA" in body(event)["issues"]
    assert "secret" not in value and "media.invalid" not in value
    assert config().bridge_token not in value


def test_cycles_fetch_limits_and_repeated_ids_preserve_order_without_duplicate_reads():
    event, protocol = enriched([forward(), forward()], {
        ("forward", "shared"): {"messages": [node("A", text("重复资料"), forward())]},
    })
    assert protocol.calls == [("forward", "shared")]
    assert all("CYCLE" in body(event, i)["issues"] for i in range(2))
    event, protocol = enriched([reply(str(i)) for i in range(1, 7)])
    assert len(protocol.calls) == 4 and "FETCH_LIMIT" in body(event, 4)["issues"]


def test_limits_truncate_within_utf8_contract_and_stop_deep_reads():
    event, _ = enriched([forward()], {
        ("forward", "shared"): {"messages": [node("昵称", text("中" * 10000))]},
    })
    assert body(event)["status"] == "TRUNCATED"
    assert "SIZE_LIMIT" in body(event)["issues"]
    assert len(qq_rich.encoded(event["payload"]["rich"]).encode()) <= MAX_RICH_BYTES
    responses = {("forward", str(i)): {"messages": [node("A", forward(str(i + 1)))]}
                 for i in range(5)}
    event, protocol = enriched([forward("0")], responses)
    assert len(protocol.calls) == 4 and "DEPTH_LIMIT" in body(event)["issues"]


def test_total_node_limit_applies_across_all_references():
    event, _ = enriched([forward(), forward("other")], {
        ("forward", "shared"): {"messages": [node("A", text(str(i))) for i in range(40)]},
    })
    assert len(body(event)["nodes"]) == 32
    assert body(event)["status"] == body(event, 1)["status"] == "TRUNCATED"


def test_real_wait_uses_one_total_timeout(monkeypatch):
    class Slow(Protocol):
        async def get_msg(self, reference):
            self.calls.append(reference)
            await asyncio.sleep(0.03)

    monkeypatch.setattr(reader, "TIMEOUT_SECONDS", 0.01)
    protocol, message = Slow(), [reply("1"), reply("2")]
    event = prepared(message)
    run(reader.enrich(event, message, protocol, lambda value: value))
    assert protocol.calls == ["1"]
    assert all(body(event, index)["issues"] == ["TIMEOUT"] for index in range(2))


def test_total_timeout_stays_exhausted_before_the_next_coarse_clock_tick(monkeypatch):
    class Slow(Protocol):
        async def get_msg(self, reference):
            self.calls.append(reference)
            await asyncio.Event().wait()

    # Windows 3.12 can expire an asyncio timer within a 15.625ms monotonic
    # tick. Keep only the reader's clock fixed; asyncio still uses its real timer.
    monkeypatch.setattr(reader, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    monkeypatch.setattr(reader, "TIMEOUT_SECONDS", 0.01)
    protocol, message = Slow(), [reply("1"), reply("2")]
    event = prepared(message)
    run(reader.enrich(event, message, protocol, lambda value: value))
    assert protocol.calls == ["1"]
    assert all(body(event, index)["issues"] == ["TIMEOUT"] for index in range(2))


@pytest.mark.parametrize("error", [TimeoutError(), httpx.ReadTimeout("upstream timeout")])
def test_upstream_timeout_keeps_the_remaining_total_budget(monkeypatch, error):
    monkeypatch.setattr(reader, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    event, protocol = enriched([reply("1"), reply("2")], {
        ("reply", "1"): error,
        ("reply", "2"): node("A", text("仍在总预算内的第二条引用")),
    })
    assert protocol.calls == [("reply", "1"), ("reply", "2")]
    assert body(event)["issues"] == ["TIMEOUT"]
    assert body(event, 1)["status"] == "READ"
    assert body(event, 1)["nodes"][0]["text"] == "仍在总预算内的第二条引用"


def test_restore_reuses_signed_projection_and_rejects_changed_owner_or_reference():
    message = [reply(), text("当前问题")]
    event, _ = enriched(message, {("reply", "-42"): node("A", text("原文"))})
    projection = event["payload"]["rich"]
    assert reader.restore(prepared(message), projection) == event
    changed = deepcopy(projection)
    changed["segments"][0]["reference"] = "-43"
    with pytest.raises(ValueError, match="CAPTURE_INPUT_CHANGED"):
        reader.restore(prepared(message), changed)


@pytest.mark.parametrize("field,value", [("status", "READ"), ("source", "owner"),
                                         ("issues", ["arbitrary"]), ("nodes", [{"text": "bad"}])])
def test_http_rejects_forged_body_status_or_extensions(field, value):
    event, _ = enriched([reply()])
    body(event)[field] = value
    with pytest.raises(ValidationError):
        InboundEvent.model_validate(event)


def test_onebot_read_action_wire_format_and_invalid_ids():
    requests = []

    def response(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"message": []}})

    async def scenario():
        async with httpx.AsyncClient(
            base_url="http://protocol", transport=httpx.MockTransport(response),
        ) as http:
            client = OneBotClient(config(), http)
            await client.get_msg("-42")
            await client.get_forward_msg("res-id")
            with pytest.raises(ValueError):
                await client.get_msg(True)
            with pytest.raises(ValueError):
                await client.get_forward_msg("https://invalid")

    run(scenario())
    assert requests == [("/get_msg", {"message_id": -42}), ("/get_forward_msg", {"id": "res-id"})]


def test_oversized_onebot_response_keeps_question_with_explicit_truncation():
    calls = []
    message = [reply(), text("请解释引用")]
    event = prepared(message)

    def response(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {
            "message": [text("unretained-upstream-body" * 4000), {
                "type": "image", "data": {"url": "https://media.invalid/refreshed-private"},
            }],
        }})

    async def scenario():
        async with httpx.AsyncClient(
            base_url="http://protocol", transport=httpx.MockTransport(response),
        ) as http:
            await reader.enrich(event, message, OneBotClient(config(), http), lambda value: value)

    run(scenario())
    InboundEvent.model_validate(event)
    assert calls == ["/get_msg"]
    assert event["payload"]["text"] == "请解释引用"
    assert body(event)["status"] == "TRUNCATED"
    assert body(event)["issues"] == ["SIZE_LIMIT"] and body(event)["nodes"] == []
    serialized = json.dumps(event)
    assert "unretained-upstream-body" not in serialized and "media.invalid" not in serialized


def test_card_type_fields_project_without_promoting_unsupported_links():
    message = [{"type": "json", "data": {"data": json.dumps({
        "prompt": "卡片简介", "meta": {
            "music": {"title": "歌名", "desc": "歌手", "jumpUrl": "https://unsupported.invalid"},
            "detail": {"title": "文章", "url": "https://xiaoheihe.cn/a/123"},
        },
    })}}]
    event, _ = enriched(message)
    fields = event["payload"]["rich"]["segments"][0]["fields"]
    assert {field["value"] for field in fields} == {
        "卡片简介", "歌名", "歌手", "文章", "https://www.xiaoheihe.cn/app/bbs/link/123",
    }


@pytest.mark.parametrize("url,canonical", [
    ("https://m.bilibili.com/video/BV1234567890?share_source=qq&token=private",
     "https://www.bilibili.com/video/BV1234567890"),
    ("https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=ABCDEF123456&token=private",
     "https://www.xiaoheihe.cn/app/bbs/link/abcdef123456"),
    ("https://www.xiaoheihe.cn/app/bbs/link/123?token=private",
     "https://www.xiaoheihe.cn/app/bbs/link/123"),
    ("https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=123&link_id=456", None),
    ("https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=https%3A%2F%2Fevil.invalid", None),
    ("https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?link_id=", None),
    ("https://evil.invalid/v3/bbs/app/api/web/share?link_id=123", None),
    ("https://b23.tv/synthetic?share_source=qq&bbid=private", "https://b23.tv/synthetic"),
])
def test_card_tracking_queries_only_yield_fixed_platform_targets(url, canonical):
    event, _ = enriched([{"type": "json", "data": {"data": json.dumps({"url": url})}}])
    fields = event["payload"]["rich"]["segments"][0]["fields"]
    assert [field["value"] for field in fields] == ([canonical] if canonical else [])
    assert "token=" not in json.dumps(event) and "private" not in json.dumps(event["payload"])


def test_malformed_fetched_segment_retains_valid_text_and_reports_partial():
    event, _ = enriched([reply()], {("reply", "-42"): node(
        "A", text("正文"), {"type": [], "data": {}},
    )})
    assert body(event)["status"] == "PARTIAL"
    assert body(event)["issues"] == ["MALFORMED"]


def test_capture_authority_failure_is_not_downgraded_to_missing_quote():
    with pytest.raises(ValueError, match="CAPTURE_PROTOCOL_BOUNDARY_CHANGED"):
        enriched([reply()], {("reply", "-42"): ValueError("CAPTURE_PROTOCOL_BOUNDARY_CHANGED")})


@pytest.mark.parametrize("suffix", ["]", ""])
def test_fetched_cq_media_never_retains_refreshed_urls_or_triggers_extra_reads(suffix):
    event, protocol = enriched([reply()], {("reply", "-42"): {
        "message": "正文[CQ:image,url=https://media.invalid/refreshed" + suffix,
    }})
    assert "media.invalid" not in json.dumps(event)
    assert body(event)["nodes"][0]["text"] == "正文"
    assert body(event)["status"] == "PARTIAL" and len(protocol.calls) == 1


def test_published_event_schema_accepts_projection_and_rejects_untrusted_extensions():
    schema = json.loads((Path(__file__).resolve().parents[1] /
                         "contracts/event.v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    event, _ = enriched([forward()], {
        ("forward", "shared"): {"messages": [node("A", text("正文"))]},
    })
    validator.validate(event)
    body(event)["nodes"][0]["actor"] = "owner"
    assert list(validator.iter_errors(event))


def test_retrieved_body_is_context_but_not_owner_memory_evidence_and_erases_with_source(
    core, tmp_path, empty_database,
):
    event, _ = enriched([reply(), text("请解释")], {
        ("reply", "-42"): node("A", text("我喜欢耳机")),
    })
    incoming = InboundEvent.model_validate(event)
    core.settings.rich_input_scope = "references_v1"
    core.ingest(incoming, incoming.event_id)
    with pytest.raises(CoreError, match="UNSUPPORTED_MEMORY_EVIDENCE"):
        core.memory.validate_candidate(Candidate(
            subject="user", kind="fact", fact_key="third-party-preference",
            content="我喜欢耳机", evidence_event_id=incoming.event_id,
            evidence_quote="我喜欢耳机",
        ), confirmed=True)
    assert run(core.run_once())
    messages, _ = core.model.calls[0]
    assert "我喜欢耳机" in messages[-2]["content"]
    assert messages[-1] == {"role": "user", "content": "请解释"}
    copy_database(core.settings.database, empty_database, tmp_path)
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [incoming.event_id], core.clock())
    assert core.store.one("SELECT body_json FROM events")["body_json"] == "{}"
    with (Database(empty_database) as db,
          closing(Store(db, core.store.state_directory)) as restored):
        core.ledger.replay(restored)
        assert restored.one("SELECT body_json FROM events")["body_json"] == "{}"
