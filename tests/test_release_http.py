"""Offline release regressions for content reads, quotas, and command dispatch."""

import gzip
import json
from contextlib import asynccontextmanager

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

from companion.content import fetch_public_image
from companion.contracts import CoreError
from companion.executive import HTTPExecutor
from companion.executive_protocol import ExecutorEndpoint
from companion.executor_budget_proxy import create_budget_proxy
from companion.media_config import MediaConfig, MediaEndpoint
from companion.media_provider import HTTPMediaProvider
from companion_onebot_bridge.images import fetch_image, original_images
from source_reader.heybox import HeyboxReader, SourceAPI
from source_reader.network import PublicHTTP, ReadBudget, ReadFailure
from tests.conftest import ingest, run
from tests.support.content import POST, enable
from tests.support.executive import TOKEN, envelope

PATHS = ("public_reader", "source_reader", "qq_image", "link_image")
BODY = json.dumps(
    {"status": "ok", "result": {"link": {"title": "Example", "text": "Article body"}}}
).encode()


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data
        self.read_chunks = 0

    async def __aiter__(self):
        for offset in range(0, len(self.data), 16):
            self.read_chunks += 1
            yield self.data[offset : offset + 16]


async def read_response(path, stream, limit, encoding=None):
    headers = {"Content-Type": "image/png"}
    if encoding is not None:
        headers["Content-Encoding"] = encoding
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers=headers, stream=stream)
    )
    if path == "qq_image":
        async def check():
            pass

        async def resolve(*args):
            return ["1.1.1.1"]

        image = original_images([{"type": "image", "data": {
            "url": "https://gchat.qpic.cn/image/one"}}])[0]
        return await fetch_image(image, limit, check=check, resolver=resolve, transport=transport)
    if path == "link_image":
        return await fetch_public_image(
            "https://imgheybox.max-c.com/image/one", "heybox", limit, transport
        )
    class Pool:
        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            assert method == "GET"
            yield httpcore.Response(200, headers=headers, content=stream)

        async def aclose(self):
            pass

    budget = ReadBudget(limit, 1)
    if path == "source_reader":
        async with SourceAPI("https://source.example", "source-token-" * 3, Pool()) as source:
            items = await HeyboxReader(None, source).read(
                {"operation": "read", "url": POST}, budget)
            return items, budget.used_bytes
    async with PublicHTTP(Pool()) as public:
        return await public.get(POST, budget, "article")


@pytest.mark.parametrize("path", PATHS)
def test_content_download_rejects_compression_before_reading_stream(path):
    stream = TrackingStream(gzip.compress(BODY))
    with pytest.raises((ValueError, CoreError, ReadFailure)):
        run(read_response(path, stream, 1024, "gzip"))
    assert stream.read_chunks == 0


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("encoding", [None, "identity"])
def test_content_download_accepts_bounded_identity_response(path, encoding):
    stream = TrackingStream(BODY)
    result = run(read_response(path, stream, len(BODY), encoding))
    assert stream.read_chunks > 0
    if path == "source_reader":
        items, used_bytes = result
        assert items[0]["text"] == "Article body"
        assert used_bytes == len(BODY)
    else:
        assert (result if path == "public_reader" else result[0]) == BODY


@pytest.mark.parametrize("path", PATHS)
def test_content_download_rejects_oversize_identity_stream(path):
    stream = TrackingStream(b"x" * 129)
    with pytest.raises((ValueError, CoreError, ReadFailure), match="BYTE_LIMIT|TOO_LARGE"):
        run(read_response(path, stream, 128, "identity"))


@pytest.mark.parametrize("allowance", [0, 1])
def test_owner_shares_respect_daily_deep_read_allowance(core, allowance):
    enable(core)
    core.settings.exploration_daily_deep_reads = allowance
    for number in range(1, allowance + 2):
        ingest(core, number, f"https://www.xiaoheihe.cn/app/bbs/link/{123456 + number}")
    for _ in range(allowance):
        assert core.exploration.lease("reader") is not None
    assert core.exploration.lease("reader") is None
    assert (
        core.store.one(
            "SELECT COUNT(*) AS n FROM exploration_requests WHERE started_at IS NOT NULL"
        )["n"]
        == allowance
    )


@pytest.mark.parametrize("command", ["/暂停自主活动", "/目标列表", "/今天写日记了吗"])
def test_content_enabled_commands_and_following_chat_complete(core, command):
    enable(core)
    ingest(core, 1, command)
    ingest(core, 2, "普通后续对话")
    assert run(core.run_once())
    assert not core.model.calls
    if command == "/暂停自主活动":
        assert (
            core.store.one("SELECT value FROM runtime_meta WHERE key='life_paused'")["value"] == "1"
        )
    assert run(core.run_once())
    assert core.store.rows('SELECT state FROM jobs ORDER BY record_sequence') == [
        {"state": "SUCCEEDED"},
        {"state": "SUCCEEDED"},
    ]
    assert core.store.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 2


def test_content_enabled_incomplete_command_falls_back_to_chat(core):
    enable(core)
    ingest(core, 1, "/安排任务")
    ingest(core, 2, "普通后续对话")
    assert run(core.run_once())
    assert run(core.run_once())
    assert core.store.rows('SELECT state FROM jobs ORDER BY record_sequence') == [
        {"state": "SUCCEEDED"},
        {"state": "SUCCEEDED"},
    ]
    assert [messages[-1]["content"] for messages, _ in core.model.calls] == [
        "/安排任务",
        "普通后续对话",
    ]
    assert core.store.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 2


def test_content_enabled_ordinary_share_still_waits_until_deadline(core):
    enable(core)
    ingest(core, text=POST)
    assert not run(core.run_once())
    assert not core.model.calls
    core.clock.advance(core.content.options.wait_seconds + 1)
    assert run(core.run_once())
    assert core.store.one("SELECT state FROM jobs")["state"] == "SUCCEEDED"


@pytest.mark.parametrize("path", ["media", "executor", "budget_proxy"])
def test_other_bounded_providers_reject_compression_before_reading(path):
    stream = TrackingStream(gzip.compress(BODY))

    def response(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)

    transport = httpx.MockTransport(response)
    if path == "media":
        provider = HTTPMediaProvider(MediaConfig(), transport)
        endpoint = MediaEndpoint(url="https://media.example/process", model="fixture")
        with pytest.raises(CoreError, match="ENCODING_UNSUPPORTED"):
            run(provider.process("audio", b"fixture", "audio/webm", endpoint))
    elif path == "executor":
        endpoint = ExecutorEndpoint(
            url="http://127.0.0.1:8991",
            token=TOKEN,
            workspace="work",
            capabilities=["windows.read_text"],
        )
        with pytest.raises(ValueError, match="ENCODING_UNSUPPORTED"):
            run(HTTPExecutor(transport).execute(endpoint, envelope()))
    else:
        app = create_budget_proxy(
            TOKEN,
            "https://model.example/v1",
            "fixture-key",
            "fixed-model",
            1,
            1,
            transport=transport,
        )
        auth = {"Authorization": "Bearer " + TOKEN}
        with TestClient(app) as client:
            grant = client.post(
                "/internal/v1/reservations", headers=auth, json=envelope().model_dump()
            ).json()
            result = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + grant["token"]},
                json={"messages": [{"role": "user", "content": "fixture"}]},
            )
            assert result.status_code == 502
            assert result.json()["detail"] == "MODEL_ENCODING_UNSUPPORTED"
            usage = client.get("/internal/v1/reservations/" + grant["token"], headers=auth).json()
            assert usage["calls"] == usage["unknown_calls"] == 1
            assert usage["billed_micro_usd"] == 0
    assert stream.read_chunks == 0
