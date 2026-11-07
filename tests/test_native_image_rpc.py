"""Real HTTP latency must leave the native image download and upload usable."""

import asyncio
import json
import time
from collections import Counter
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

import httpx

from companion_onebot_bridge import pending_media
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from tests.conftest import run
from tests.support.native_image_bursts import model_images
from tests.support.native_image_pipeline import pixels
from tests.support.native_media_queue import captured, row
from tests.support.onebot import provider
from tests.support.owner_image_recovery import run_worker
from tests.support.prepare_owner_input import native_spool, window


def test_image_two_point_six_second_core_roundtrips_reach_sdk_and_ack(core, tmp_path):
    folder = Path(".local/image-rpc-verification")
    folder.mkdir(parents=True, exist_ok=True)

    async def scenario():
        core.settings.content = core.settings.content.model_copy(update={"wait_seconds": 60})
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            spool, runtime = native_spool(core, tmp_path, config, plan)
            downloads, claims, requests, phases, sends = [], [], [], [], []
            anchor, started = None, None

            async def ticking():
                while True:
                    core.clock.now = anchor + timedelta(seconds=time.monotonic() - started)
                    await asyncio.sleep(0.005)

            async def resolve(*_):
                return ["1.1.1.1"]

            async def download(request):
                await request.extensions["trace"]("http11.send_request_headers.started", {})
                downloads.append(request)
                return httpx.Response(200, content=pixels(), headers={"Content-Type": "image/png"})

            async def latency(response):
                path = response.request.url.path
                requests.append(path)
                if path.endswith("/image/claim"):
                    await response.aread()
                    claims.append(response.json())
                await asyncio.sleep(2.6)

            ticker, error_class = None, None
            try:
                await captured(core, client, runtime)
                anchor, started = core.clock.now, time.monotonic()
                ticker = asyncio.create_task(ticking())
                async with private_http_client(config.core_url, config.bridge_token) as transfer:
                    transfer.event_hooks["response"].append(latency)
                    pump = pending_media.MediaPump(runtime, transfer,
                        image_transport=httpx.MockTransport(download), image_resolver=resolve,
                        set_phase=phases.append)
                    try:
                        await pump.execute(row(runtime))
                    except Exception as error:
                        error_class = type(error).__name__
                        raise
                    finally:
                        index = 0
                        while (folder / f"measurement-{index}.json").exists():
                            index += 1
                        (folder / f"measurement-{index}.json").write_text(json.dumps({
                            "delay_seconds": 2.6, "elapsed_seconds": time.monotonic() - started,
                            "requests": dict(Counter(requests)), "phases": phases,
                            "GET_headers_sent": len(downloads), "claims": len(claims),
                            "error_class": error_class,
                            "image_state": core.store.one(
                                "SELECT state FROM content_items WHERE kind='qq_image'")["state"],
                        }, indent=2) + "\n", encoding="utf-8")
                ticker.cancel()
                with suppress(asyncio.CancelledError):
                    await ticker
                assert len(downloads) == len(claims) == 1
                assert sum(path.endswith("/image/check") for path in requests) == 4
                # Image checks carry their complete authority receipt in the same RPC.
                assert requests.count("/internal/v1/owner-authority/check") == 5
                saved = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
                assert saved["state"] == "SUCCEEDED"
                assert saved["lease_token"] == claims[0]["lease_token"]
                assert saved["lease_until"] == claims[0]["lease_until"]
                assert row(runtime)["state"] == "DONE"
                wire = await run_worker(core, tmp_path, 1, "原图已收到。")
                assert model_images(wire[0]) == [pixels()]
                async with private_http_client(config.onebot_http_url, config.onebot_token,
                    transport=httpx.MockTransport(lambda request: provider(request, sends)),
                ) as protocol:
                    guard = OwnerSendGuard(runtime, client)
                    assert await deliver_tick(client, OwnerOneBot(guard, protocol), spool,
                                              guard=guard)
                assert len(sends) == 1
                assert core.store.one("SELECT state FROM outbox")["state"] == "SENT"
                assert spool.db.execute("SELECT state FROM window_attempts").fetchone()[0] == "ACK"
                assert core.store.rows("SELECT state FROM model_usage") == [{"state": "SETTLED"}]
            finally:
                if ticker is not None:
                    ticker.cancel()
                    with suppress(asyncio.CancelledError):
                        await ticker
                spool.close()
    run(scenario())
