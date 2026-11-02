"""Transport fragmentation must not multiply private authorization round trips."""

import asyncio

import httpx
import pytest

from companion_meme_sources import DiscoveryLimits, Usage
from companion_meme_sources import fetch as fetch_module
from companion_meme_sources.fetch import PublicFetcher
from companion_onebot_bridge.meme_runtime import MemeRuntime, failure_diagnostic
from companion_onebot_bridge.meme_semantic_http import SemanticHTTP
from tests.conftest import run
from tests.support.native_meme import NativeCase
from tests.test_meme_runtime import RULE
from tests.test_next_memes_discovery import resolver


def test_fragmented_body_finishes_with_bounded_authorization_latency(monkeypatch):
    original_timeout = asyncio.timeout
    monkeypatch.setattr(fetch_module.asyncio, "timeout", lambda seconds: original_timeout(0.15))
    body = b"a" * 16384
    usage = Usage()

    class Fragments(httpx.AsyncByteStream):
        async def __aiter__(self):
            for start in range(0, len(body), 128):
                yield body[start:start + 128]

    async def private_check():
        await asyncio.sleep(0.005)

    fetcher = PublicFetcher(DiscoveryLimits(), usage, private_check,
        resolver=resolver, transport=httpx.MockTransport(lambda request: httpx.Response(
            200, stream=Fragments(), headers={"content-type": "application/json"})))
    result = run(fetcher.get("https://example.org/listing", 65536))
    assert result.data == body
    assert usage.requests == 1 and usage.download_bytes == len(body)


@pytest.mark.parametrize("failure", ["partial", "cancel", "limit", "withdrawn"])
def test_buffered_reads_retain_observed_usage_and_never_issue_invalid_evidence(failure):
    usage = Usage()
    stopped = False
    closed = False
    observed = 0

    class Fragments(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal stopped, observed
            for index in range(100):
                if index == 3 and failure in {"partial", "cancel"}:
                    if failure == "cancel":
                        raise asyncio.CancelledError()
                    raise httpx.ReadError("synthetic partial body")
                stopped = failure == "withdrawn"
                observed += 128
                yield b"a" * 128

        async def aclose(self):
            nonlocal closed
            closed = True

    async def check():
        if stopped:
            raise ValueError("RECALLED")

    fetcher = PublicFetcher(DiscoveryLimits(), usage, check, resolver=resolver,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Fragments())))
    error = {"partial": httpx.ReadError, "cancel": asyncio.CancelledError,
             "limit": ValueError, "withdrawn": ValueError}[failure]
    with pytest.raises(error):
        run(fetcher.get("https://example.org/listing", 300 if failure == "limit" else 65536))
    assert closed and usage.download_bytes == observed > 0
    assert fetcher._issued == []


def test_native_profile_uses_one_current_authorization_at_dispatch(core, tmp_path):
    fixture = NativeCase(core, tmp_path, rules=(RULE,), semantic=True)
    runtime = MemeRuntime(fixture.owner)
    checks = []

    async def scenario():
        async with fixture.client(runtime) as client:
            async def observe(request):
                if request.url.path == "/internal/v1/owner-authority/check":
                    checks.append(request.url.path)

            client.event_hooks["request"].append(observe)
            result = await SemanticHTTP(runtime.window, client,
                check=runtime._owner_check).profile()
            assert result[0].model_version and result[1].dimensions == 512

    try:
        run(scenario())
        assert len(checks) == 1
    finally:
        fixture.spool.close()


@pytest.mark.parametrize("error,reason", [
    (TimeoutError("secret text"), "TIMEOUT"),
    (httpx.ReadError("secret endpoint"), "HTTP_TRANSPORT"),
    (ValueError("private source"), "CHECK_REJECTED"),
    (RuntimeError("private body"), "INTERNAL_ERROR"),
])
def test_failure_diagnostics_never_export_exception_text(error, reason):
    assert failure_diagnostic(error) == {"reason": reason}


def test_fragmented_acquisition_caption_retrieval_and_native_qq_image(core, tmp_path, monkeypatch):
    from tests import test_meme_semantic_integration as integration

    original = integration.public_transport

    class Fragments(httpx.AsyncByteStream):
        def __init__(self, body):
            self.body = body

        async def __aiter__(self):
            for byte in self.body:
                yield bytes([byte])

    def public_transport(calls, **kwargs):
        delegate = original(calls, **kwargs)

        async def handle(request):
            response = await delegate.handle_async_request(request)
            return httpx.Response(response.status_code, headers=response.headers,
                stream=Fragments(await response.aread()))

        return httpx.MockTransport(handle)

    monkeypatch.setattr(integration, "public_transport", public_transport)
    integration.test_signed_capture_caption_index_current_offer_and_original_image_delivery(
        core, tmp_path, handoff=False, deferred=False, delayed=False)
