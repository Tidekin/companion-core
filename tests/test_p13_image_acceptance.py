"""Partial CDN bodies must stop the existing image pipeline before paid I/O."""

import json
import logging

import httpx
import pytest

from tests.conftest import run
from tests.support.native_images import image_host, raw_event, upload
from tests.test_live_image_input import options, png
from tests.test_onebot_images import URL, image_segment


@pytest.mark.parametrize("failure", ["read_timeout", "stream_over_limit"])
def test_partial_download_is_closed_once_without_model_or_fallback_reply(
    core, tmp_path, caplog, failure,
):
    downloads, calls, closed = [], [], []
    caplog.set_level(logging.DEBUG)

    class PartialBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            prefix = png()[:16]
            yield prefix
            core.clock.advance(61)
            await core.content.run_once()
            if failure == "read_timeout":
                raise httpx.ReadTimeout(URL)
            # Cross the original download limit by one byte despite the small header.
            yield b"x" * (core.settings.content.max_image_bytes + 1 - len(prefix))
            raise AssertionError("The rejected image stream must not be consumed further")

        async def aclose(self):
            closed.append(True)

    async def source(ws):
        await ws.send(json.dumps(raw_event(message=[image_segment()])))
        await ws.wait_closed()

    def cdn(request):
        downloads.append(request)
        return httpx.Response(200, stream=PartialBody(), headers={
            "Content-Type": "image/png", "Content-Length": "100",
        })

    def model(request):
        calls.append(request)
        raise AssertionError("An incomplete image must never reach the provider")

    async def resolve(host, port):
        return ["1.1.1.1"]

    async def scenario():
        async with image_host(core, tmp_path, source, options=options(), model_reply=model) as (
            active, _, sends, _,
        ):
            await active.capture()
            await active.flush()
            await upload(active, transport=httpx.MockTransport(cdn), resolver=resolve)
            assert active.db.execute("SELECT state FROM automatic_media").fetchone()[0] == (
                "DONE")
            assert core.store.one("SELECT state FROM content_items")["state"] == "FAILED"
            assert len(downloads) == 1 and closed == [True]
            assert calls == sends == []
            assert not core.store.one("SELECT 1 FROM model_usage")
            assert not core.store.one("SELECT 1 FROM model_admissions")
            assert not core.store.one("SELECT 1 FROM observations")
            assert not core.store.one("SELECT 1 FROM outbox")
            assert core.store.one("SELECT COUNT(*) n FROM content_items")["n"] == 1
            core.clock.advance(181)
            active.restart()
            await upload(active, transport=httpx.MockTransport(cdn), resolver=resolve)
            assert len(downloads) == 1 and calls == sends == []

    run(scenario())
    assert URL not in caplog.text and "synthetic-private-reference" not in caplog.text
