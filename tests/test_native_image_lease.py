"""Native image claims retain one bounded lease through slow Core roundtrips."""

import base64
from datetime import datetime, timedelta

import httpx
import pytest

from companion import agent_images
from companion.core import Core
from companion_onebot_bridge import pending_media
from companion_onebot_bridge.network import private_http_client
from tests.conftest import run
from tests.support.owner_image_recovery import run_worker
from tests.support.prepare_owner_input import native_spool, window
from tests.test_native_image_pipeline import admit_image, claim, pixels, upload_headers
from tests.test_native_media_queue import captured, row


@pytest.mark.parametrize("claim_delay", [0, 65])
def test_claim_is_capped_at_sixty_seconds_and_original_deadline(core, tmp_path, claim_delay):
    async def scenario():
        core.settings.content = core.settings.content.model_copy(update={"wait_seconds": 90})
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, image = await admit_image(core, client, config, plan, tmp_path)
            core.clock.advance(claim_delay)
            before = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
            claimed_at = core.clock.now
            receipt = await claim(client, event, image)
            expected = min(datetime.fromisoformat(before["deadline"]),
                           claimed_at + timedelta(seconds=60))
            assert datetime.fromisoformat(receipt["lease_until"]) == expected
            core.clock.advance(16)
            check_path = "/internal/v2/content/" + event["event_id"] + "/image/check"
            check_body = {"lease_token": receipt["lease_token"],
                          "source_sha256": image["source_sha256"]}
            response = await client.post(check_path, json=check_body)
            assert response.status_code == 200, response.text
            repeated = await client.post(check_path.removesuffix("check") + "claim", json={
                "request_key": "image:0", "source_sha256": image["source_sha256"]})
            assert repeated.status_code == 409
            saved = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
            assert saved["lease_token"] == receipt["lease_token"]
            assert saved["lease_until"] == receipt["lease_until"]
            assert saved["deadline"] == before["deadline"]
            core.clock.now = expected + timedelta(milliseconds=1)
            response = await client.post(check_path, json=check_body)
            assert response.status_code == 409
            with Core(core.settings, clock=core.clock) as independent:
                await independent.content.run_once()
            expired = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
            assert expired["state"] == "FAILED"
            assert expired["error_code"] == "CONTENT_IMAGE_DOWNLOAD_EXPIRED"
            for key in ("lease_token", "lease_until", "reserved_bytes", "deadline"):
                assert expired[key] == saved[key]
            assert not core.store.rows("SELECT * FROM model_usage")
            assert not core.store.rows("SELECT * FROM captured_images")
    run(scenario())


def test_slow_roundtrips_preserve_one_claim_and_original_in_independent_sdk_worker(core, tmp_path):
    async def scenario():
        core.settings.content = core.settings.content.model_copy(update={"wait_seconds": 90})
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            spool, runtime = native_spool(core, tmp_path, config, plan)
            requests, downloads, claims = [], [], []

            async def resolve(*_):
                return ["1.1.1.1"]

            async def download(request):
                await request.extensions["trace"]("http11.send_request_headers.started", {})
                downloads.append(request)
                return httpx.Response(200, content=pixels(), headers={"Content-Type": "image/png"})

            async def latency(response):
                requests.append(response.request.url.path)
                if response.request.url.path.endswith("/image/claim"):
                    await response.aread()
                    claims.append(response.json())
                core.clock.advance(1.6)

            try:
                await captured(core, client, runtime)
                async with private_http_client(config.core_url, config.bridge_token) as transfer:
                    transfer.event_hooks["response"].append(latency)
                    pump = pending_media.MediaPump(runtime, transfer,
                        image_transport=httpx.MockTransport(download), image_resolver=resolve)
                    await pump.execute(row(runtime))
                saved = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
                assert saved["state"] == "SUCCEEDED" and len(downloads) == len(claims) == 1
                assert sum(path.endswith("/image/claim") for path in requests) == 1
                assert saved["lease_token"] == claims[0]["lease_token"]
                assert saved["lease_until"] == claims[0]["lease_until"]
                assert row(runtime)["state"] == "DONE"
                assert not core.store.rows("SELECT * FROM model_usage")
            finally:
                spool.close()
            wire = await run_worker(core, tmp_path, 1, "收到了原图。")
            images = [part for message in wire[0]["messages"]
                      if isinstance(message["content"], list) for part in message["content"]
                      if part["type"] == "image_url"]
            assert len(images) == 1
            assert base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1]) == pixels()
            assert core.store.rows("SELECT state FROM model_usage") == [{"state": "SETTLED"}]
    run(scenario())


@pytest.mark.parametrize("history", [False, True])
def test_two_images_wait_only_for_permitted_history_and_release_failed_original(
    core, tmp_path, history,
):
    async def scenario():
        core.settings.content = core.settings.content.model_copy(update={"wait_seconds": 90})
        async with window(core, tmp_path, media=True, content=True, history=history,
                          image_count="unlimited") as (client, config, plan):
            first, first_image = await admit_image(core, client, config, plan, tmp_path, number=41)
            core.clock.advance(1)
            second, image = await admit_image(core, client, config, plan, tmp_path, number=42)
            old_job = core.store.one("SELECT * FROM jobs WHERE event_id=?", (first["event_id"],))
            assert old_job["state"] == "CANCELLED"
            receipt = await claim(client, second, image)
            response = await client.post("/internal/v2/content/" + second["event_id"] + "/image",
                content=pixels(), headers=upload_headers(plan, second, image, receipt, pixels()))
            assert response.status_code == 200, response.text
            job = core.store.one("SELECT j.*,e.conversation_id FROM jobs j JOIN events e "
                                 "USING(event_id) WHERE event_id=?", (second["event_id"],))
            assert agent_images.waiting(core, job) is history
            original = core.store.one("SELECT * FROM content_items WHERE owner_event_id=?",
                                      (first["event_id"],))
            assert original["state"] == "WAITING"
            if history:
                assert core.claim_job("new-image-before-old-deadline", job_id=job["job_id"]) is None
                old_lease = await claim(client, first, first_image)
                assert agent_images.waiting(core, job)
                core.clock.now = (datetime.fromisoformat(old_lease["lease_until"])
                                  + timedelta(milliseconds=1))
                with Core(core.settings, clock=core.clock) as independent:
                    await independent.content.run_once()
                failed = core.store.one("SELECT * FROM content_items WHERE owner_event_id=?",
                                        (first["event_id"],))
                assert failed["state"] == "FAILED"
                assert failed["error_code"] == "CONTENT_IMAGE_DOWNLOAD_EXPIRED"
                assert core.clock.now < datetime.fromisoformat(failed["deadline"])
                assert not agent_images.waiting(core, job)
            assert core.claim_job("new-image-after-media", job_id=job["job_id"]) is not None
    run(scenario())
