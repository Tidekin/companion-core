"""A real QQ miniapp shape reaches a bounded Reader without widening owner authority."""

import hashlib
import json

import httpx
import pytest
from pydantic import ValidationError

from companion import owner_authority
from companion.config import ObservationAdapterConfig
from companion.contracts import InboundEvent
from companion.exploration import ReadResult
from companion.model import ModelClient
from companion.observation import ReadingEvidence, ShortLinkResolution
from companion.owner_authority_protocol import AuthorityStop
from companion.sources import content_target
from source_reader.bili_tools import BilibiliAccountHTTP
from source_reader.bilibili import BilibiliReader
from source_reader.network import PublicHTTP, ReadBudget, ReadFailure, redirect_video_url
from source_reader.worker_v1 import tick
from tests.conftest import run
from tests.support.exploration import URL
from tests.support.native_consumer import source_window
from tests.support.onebot_events import raw_event
from tests.support.reading_followup import select_read
from tests.support.source_reader import ITEM, META, PLAYER, SUBTITLE, read, response
from tests.test_heybox_reader import mock_pool
from tests.test_live_card_content import options
from tests.test_plugin_readonly import NOW_DT, SECRET, grant
from tests.test_qq_rich_input import rich_event

SHORT = "https://b23.tv/Synthetic7"
LOCATION = URL + "?share_source=qq&tracking=private-tracking-value"


def miniapp():
    return {"type": "json", "data": {"data": json.dumps({
        "app": "com.tencent.miniapp_01", "meta": {"detail_1": {
            "title": "合成视频标题", "desc": "卡片摘要不是正文",
            "qqdocurl": SHORT + "?bbid=private-owner-token&share_medium=android"
            "&share_source=qq&ts=1234567890",
            "icon": "https://example.invalid/icon.png",
            "preview": "https://i0.hdslb.com/bfs/archive/private-image-key.jpg",
        }},
    })}}


def test_real_miniapp_shape_preserves_only_bounded_short_target():
    event = rich_event([miniapp()])
    InboundEvent.model_validate(event)
    fields = event["payload"]["rich"]["segments"][0]["fields"]
    assert [field for field in fields if field["kind"] == "url"] == [{
        "path": "/meta/detail_1/qqdocurl", "kind": "url", "value": SHORT,
    }]
    assert content_target(SHORT + "?share_source=qq") == ("bilibili", SHORT)
    assert "private-" not in json.dumps(event)
    assert event["payload"]["text"] == "" and event["payload"]["shared_content"] == []


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_one_redirect_uses_same_budget_and_retains_separate_resolution(status):
    result, backend = run(read(
        response(status=status, headers={"Location": LOCATION}),
        response(META), response(PLAYER), response(SUBTITLE), item={**ITEM, "url": SHORT},
    ))
    assert result["status"] == "SUCCESS" and result["used_requests"] == 4
    item = result["items"][0]
    evidence = ReadingEvidence.model_validate(item["reading"])
    assert item["url"] == SHORT and evidence.level == "L2"
    assert evidence.resolution.model_dump() == {
        "source_url": SHORT, "target_url": URL, "status": status,
        "location_sha256": hashlib.sha256(LOCATION.encode()).hexdigest(),
    }
    assert len(backend.requests) == 4 and backend.hosts[0] == "b23.tv"
    assert b"GET /Synthetic7 HTTP/1.1" in backend.requests[0]
    assert all(b"cookie:" not in value.lower() for value in backend.requests)
    assert "private-tracking-value" not in json.dumps(result)
    assert result["used_bytes"] > sum(source["bytes"] for source in item["reading"]["sources"])


@pytest.mark.parametrize("location", [
    SHORT, "https://b23.tv/again", "http://www.bilibili.com/video/BV1xx411c7mD",
    "https://127.0.0.1/video/BV1xx411c7mD", "https://www.bilibili.com.evil.invalid/video/BV1xx411c7mD",
    "https://user:pass@www.bilibili.com/video/BV1xx411c7mD",
    "https://www.bilibili.com:444/video/BV1xx411c7mD", URL + "#fragment",
    "https://www.bilibili.com/read/cv123", "https://www.bilibili.com/video/%42V1xx411c7mD",
    "https://www.bilibili.com\\@evil.invalid/video/BV1xx411c7mD", URL + "," + URL,
    URL + "?track=a," + URL, "//www.bilibili.com/video/BV1xx411c7mD",
])
def test_redirect_cannot_escape_video_target_or_chain(location):
    result, backend = run(read(
        response(status=302, headers={"Location": location}), item={**ITEM, "url": SHORT},
    ))
    assert result["status"] == "FAILED" and result["items"] == []
    assert result["used_requests"] == len(backend.requests) == 1


@pytest.mark.parametrize("location", [URL + "\n", URL + "\t", URL + "\x00", URL + "\x7f"])
def test_redirect_parser_rejects_control_characters(location):
    with pytest.raises(ReadFailure):
        redirect_video_url(location)


@pytest.mark.parametrize("reply", [
    response(status=200, headers={"Location": URL}),
    response(status=302),
    response(status=302, headers={"Location": URL, "location": URL}),
    response(status=302, headers={"Location": URL, "X-Padding": "x" * 8193}),
    response(status=403, headers={"Location": URL}),
])
def test_invalid_or_denied_redirect_never_fetches_target(reply):
    result, backend = run(read(reply, item={**ITEM, "url": SHORT}))
    assert result["status"] == "FAILED" and result["items"] == []
    assert len(backend.requests) == 1


def test_redirect_consumes_the_existing_request_allowance():
    result, backend = run(read(
        response(status=302, headers={"Location": URL}),
        item={**ITEM, "url": SHORT, "max_requests": 1},
    ))
    assert result["error_code"] == "READER_REQUEST_LIMIT" and len(backend.requests) == 1
    assert result["used_requests"] == 1 and result["items"] == []


def test_optional_resolution_preserves_old_reading_receipt_serialization():
    result, _ = run(read(response(META), response(PLAYER), response(SUBTITLE)))
    serialized = ReadResult.model_validate(result).model_dump(mode="json")
    assert "resolution" not in serialized["items"][0]["reading"]
    with pytest.raises(ValidationError):
        ShortLinkResolution(
            source_url=SHORT, target_url=SHORT, status=302, location_sha256="a" * 64,
        )


@pytest.mark.parametrize("change", ["none", "revoke", "blocked"])
def test_account_short_read_has_no_cookie_and_revalidates_before_send(tmp_path, change):
    async def scenario():
        path = grant(tmp_path, "bilibili")
        pool, backend = mock_pool(response(status=302, headers={"Location": URL}))
        async with BilibiliAccountHTTP(path, pool, clock=lambda: NOW_DT) as account:
            calls = []

            async def validate():
                calls.append("validate")
                if change == "revoke":
                    grant(tmp_path, "bilibili", enabled=False)

            budget = ReadBudget(50000, 4, validate_account_read=validate)
            if change == "blocked":
                account.verify(budget)
                account.blocked_grant = budget.authorization
            if change == "none":
                target, _ = await account.resolve_bilibili_short(SHORT, budget)
                assert target == URL and calls == ["validate"]
                assert len(backend.requests) == 1
                assert SECRET.encode() not in backend.requests[0]
                assert b"cookie:" not in backend.requests[0].lower()
            else:
                with pytest.raises(ReadFailure):
                    await account.resolve_bilibili_short(SHORT, budget)
                assert not backend.requests

    run(scenario())


@pytest.mark.parametrize("change", ["none", "stop", "erase"])
def test_signed_short_card_reader_context_and_mid_read_revocation(core, tmp_path, change):
    scope = options()
    scope["core_options"]["observation_adapters"] = {
        "reader": ObservationAdapterConfig(
            source="bilibili", token="synthetic-short-reader-token", reader_max_requests=4,
        ),
    }
    message = [{"type": "text", "data": {"text": "请解释这张分享卡片"}}, miniapp()]

    async def source(ws):
        await ws.send(json.dumps(raw_event(time=int(core.clock.now.timestamp()), message=message)))
        await ws.wait_closed()

    async def scenario():
        async with source_window(
            core, tmp_path, source, scope_options=scope,
        ) as (window, client, protocol, _, sends):
            core.content.options = core.settings.content
            await window.capture_once(client, protocol)
            assert await window.runtime.flush_one(client)
            await select_read(core, SHORT)
            event = core.store.one("SELECT * FROM events WHERE source='qq.onebot'")
            assert not await core.run_once()
            pool, backend = mock_pool(
                response(status=302, headers={"Location": LOCATION}),
                response(META), response(PLAYER), response(SUBTITLE),
            )
            original_connect = backend.connect_tcp

            async def connect(*args, **kwargs):
                if len(backend.hosts) == 1 and change != "none":
                    if change == "stop":
                        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                            operation_id="stop-short-card", authority=window.runtime.authority))
                    else:
                        with core.store.transaction():
                            core.ledger.expire_events(core.store, [event["event_id"]], core.clock())
                return await original_connect(*args, **kwargs)

            backend.connect_tcp = connect
            authorization = client.headers["Authorization"]
            client.headers["Authorization"] = "Bearer synthetic-short-reader-token"
            try:
                async with PublicHTTP(pool) as public:
                    assert await tick(client, BilibiliReader(public, clock=core.clock), "reader",
                                      source="bilibili", clock=lambda: core.clock.now)
            finally:
                client.headers["Authorization"] = authorization
            if change != "none":
                assert len(backend.requests) == 1
                assert not core.store.one("SELECT 1 FROM observations")
                assert core.store.one("SELECT state FROM reading_attempts")["state"] == "UNKNOWN"
                retained = core.store.one("SELECT * FROM exploration_requests")
                assert retained["used_requests"] is None and retained["used_bytes"] is None
                assert retained["reserved_requests"] == 4 and retained["reserved_bytes"] > 0
                assert not core.model.calls and not sends
                return
            assert len(backend.requests) == 4
            assert core.store.one("SELECT target_url FROM content_items")["target_url"] == SHORT
            evidence = json.loads(core.store.one("SELECT evidence_json FROM observations")[
                "evidence_json"
            ])["read_observation"]["evidence"]
            assert evidence["resolution"]["target_url"] == URL and evidence["level"] == "L2"
            calls = []

            def model(request):
                calls.append(json.loads(request.content))
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": "字幕讨论恒星的颜色。"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                })

            async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as model_http:
                core.model = ModelClient(core.settings, model_http)
                assert await core.run_once()
            assert len(calls) == 1
            context = json.dumps(calls[0]["messages"], ensure_ascii=False)
            assert "恒星的颜色与温度有关" in context and "未完整观看" in context
            assert "private-tracking-value" not in context and not sends

    run(scenario())
