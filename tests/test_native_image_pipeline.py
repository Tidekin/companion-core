"""Native image bytes enter through HTTP and are consumed by the real SDK worker."""

import asyncio
import hashlib
import io
import json
import pickle
import sys
from pathlib import Path

import httpx
import pytest
from PIL import Image

from companion import image_spool, owner_authority
from companion.media_config import ContentConfig
from companion.owner_authority_protocol import AuthorityStop
from companion.store import encode
from companion_onebot_bridge.capture import prepare_owner_input
from companion_onebot_bridge.images import OriginalImage, upload_original
from companion_onebot_bridge.network import private_http_client
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, worker_for
from tests.support.failure_notice import assert_failure_notice
from tests.support.onebot_events import raw_event
from tests.support.prepare_owner_input import native_spool, window


def pixels(mime="image/png"):
    stream = io.BytesIO()
    first = Image.new("RGB", (8, 8), "teal")
    if mime == "image/png":
        first.save(stream, format="PNG")
    else:
        first.save(stream, format="GIF" if mime == "image/gif" else "PNG", save_all=True,
                   append_images=[Image.new("RGB", (8, 8), "orange")], duration=120, loop=0)
    return stream.getvalue()


async def admit_image(core, client, config, plan, tmp_path, *, message=None, number=42):
    spool, runtime = native_spool(core, tmp_path, config, plan)
    try:
        frame = encode(raw_event(number=number, time=int(core.clock.now.timestamp()),
            message=message or [
                {"type": "image", "data": {"url": "https://gchat.qpic.cn/original"}}]))
        prepared = await prepare_owner_input(runtime, client, None, frame, core.clock.now,
                                              ("127.0.0.1", 3001))
        event = prepared["event"]
        assert runtime.persist_capture(event, frame, received_at=core.clock.now,
                                       peer=("127.0.0.1", 3001))
        assert await runtime.flush_one(client)
        assert not await runtime.flush_one(client)
        return event, next(iter(prepared["media"]["images"]), None)
    finally:
        spool.close()


async def claim(client, event, image):
    response = await client.post("/internal/v2/content/" + event["event_id"] + "/image/claim",
        json={"request_key": "image:0", "source_sha256": image["source_sha256"]})
    assert response.status_code == 200, response.text
    return response.json()


def upload_headers(plan, event, image, lease, body, mime="image/png"):
    from companion_delivery_protocol.authority import image_upload_signing_payload

    value = {"authority": plan.authority, "event_id": event["event_id"], "slot": 0,
        "source_sha256": image["source_sha256"], "lease_token": lease["lease_token"],
        "media_sha256": hashlib.sha256(body).hexdigest(), "mime": mime}
    value["signature"] = plan.key.sign(image_upload_signing_payload(value)).hex()
    return {"Content-Type": mime, "X-Content-Key": "image:0",
            "X-Content-Lease": lease["lease_token"], "X-Content-Proof": encode(value)}


@pytest.mark.parametrize("mime,at_limit", [
    ("image/png", False), ("image/gif", False), ("image/apng", False), ("image/png", True),
])
def test_native_upload_is_one_shot_and_sdk_consumes_image(core, tmp_path, mime, at_limit):
    async def scenario():
        if at_limit:
            core.settings.content = ContentConfig(max_image_bytes=32 * 1024**2,
                                                 max_daily_image_bytes=800 * 1024**2)
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, image = await admit_image(core, client, config, plan, tmp_path)
            job = core.store.one("SELECT * FROM jobs WHERE event_id=?", (event["event_id"],))
            assert core.claim_job("wait-for-original", job_id=job["job_id"]) is None
            body = pixels(mime)
            if at_limit:
                # A valid PNG with an ancillary chunk just before IEND.
                import zlib

                padding = bytes(core.settings.content.max_image_bytes - len(body) - 12)
                chunk = b"paDd" + padding
                body = (body[:-12] + len(padding).to_bytes(4, "big") + chunk
                        + zlib.crc32(chunk).to_bytes(4, "big") + body[-12:])
            url = "/internal/v2/content/" + event["event_id"] + "/image"
            downloads, phases = [], []

            async def download(request):
                downloads.append(request)
                return httpx.Response(200, content=body, headers={"content-type": mime})

            async def resolve(host, port):
                return ["8.8.8.8"]

            async def progress(state, receipt):
                phases.append(state)

            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                async with private_http_client(config.core_url, config.bridge_token) as transfer:
                    result = await upload_original(runtime, transfer, event["event_id"],
                        OriginalImage(**image), progress=progress,
                        transport=httpx.MockTransport(download), resolver=resolve)
                assert result["input_ready"]
            finally:
                spool.close()
            assert len(downloads) == 1
            assert phases == ["CLAIMING", "DOWNLOADING", "UPLOADING"]
            row = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
            lease = {"lease_token": row["lease_token"]}
            headers = upload_headers(plan, event, image, lease, body, mime)
            captured = core.store.one("SELECT * FROM captured_images")
            duplicate = await client.post(url, content=body, headers=headers)
            assert duplicate.status_code == 200 and duplicate.json()["duplicate"]
            assert core.store.one("SELECT * FROM captured_images") == captured
            assert not core.store.one("SELECT 1 FROM model_usage")
            config_path, output = tmp_path / "image-worker.pickle", tmp_path / "image-wire.json"
            config_path.write_bytes(pickle.dumps((core.settings, core.clock.now, job["job_id"],
                [answer("看到了原图。")], "SUCCEEDED", str(output))))
            process = await asyncio.create_subprocess_exec(sys.executable,
                str(Path(__file__).with_name("reader_sdk_worker.py")), str(config_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                logs, _ = await asyncio.wait_for(process.communicate(), 45)
            except BaseException:
                process.kill()
                await process.wait()
                raise
            assert process.returncode == 0, logs.decode(errors="replace")
            requests = json.loads(output.read_text(encoding="utf-8"))
            assert len(requests) == 1
            images = [part for message in requests[0]["messages"]
                      if isinstance(message["content"], list) for part in message["content"]
                      if part["type"] == "image_url"]
            import base64

            assert len(images) == 1
            from companion.image_frames import prepare

            model_policy = core.settings.paid_model.image_input
            expected = prepare(body, mime, model_policy,
                               original_limit=core.settings.content.max_image_bytes)
            if at_limit:
                assert row["reserved_bytes"] == len(body) == 32 * 1024**2
                assert len(expected.data) <= model_policy.max_bytes_per_image
                with Image.open(io.BytesIO(expected.data)) as decoded:
                    assert decoded.convert("RGB").getpixel((0, 0)) == (0, 128, 128)
            assert base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1]) == expected.data
            assert core.store.rows("SELECT scenario,state FROM model_usage") == [
                {"scenario": "chat", "state": "SETTLED"}]
            saved = core.store.one("SELECT conversation_json FROM agent_conversations")
            assert "data:image" not in saved["conversation_json"]
            assert "companion-image://" in saved["conversation_json"]
            assert len(core.store.rows("SELECT * FROM outbox")) == 1
            image_spool.cleanup(core.store, core.clock(), retain_completed=True)
            row = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
            assert image_spool.get(core.store, row, core.clock(), len(body))[0] == body
    run(scenario())


@pytest.mark.parametrize("boundary", ["dns_stop", "upload_header", "lost_receipt"])
def test_native_bridge_transfer_fences_final_io(core, tmp_path, boundary):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, image = await admit_image(core, client, config, plan, tmp_path)
            downloads, uploads, phases = [], [], []

            async def resolve(host, port):
                if boundary == "dns_stop":
                    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                        operation_id="stop-during-image-dns", authority=plan.authority))
                return ["8.8.8.8"]

            async def download(request):
                downloads.append(request)
                return httpx.Response(200, content=pixels(), headers={"content-type": "image/png"})

            async def request_hook(request):
                if request.url.path.endswith("/image"):
                    uploads.append(request.url.path)
                    if boundary == "upload_header":
                        request.headers["X-Content-Proof"] = "{}"

            async def response_hook(response):
                if response.request.url.path.endswith("/image") and boundary == "lost_receipt":
                    raise httpx.ReadTimeout("synthetic lost receipt", request=response.request)

            async def progress(state, receipt):
                phases.append(state)

            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                async with private_http_client(config.core_url, config.bridge_token) as transfer:
                    transfer.event_hooks["request"].append(request_hook)
                    transfer.event_hooks["response"].append(response_hook)
                    with pytest.raises((httpx.HTTPError, ValueError)):
                        await upload_original(runtime, transfer, event["event_id"],
                            OriginalImage(**image), progress=progress,
                            transport=httpx.MockTransport(download), resolver=resolve)
            finally:
                spool.close()
            assert len(downloads) == (0 if boundary == "dns_stop" else 1)
            assert len(uploads) == (0 if boundary == "dns_stop" else 1)
            assert phases == (["CLAIMING", "DOWNLOADING"] if boundary == "dns_stop" else
                              ["CLAIMING", "DOWNLOADING", "UPLOADING"])
            assert len(core.store.rows("SELECT * FROM captured_images")) == (
                1 if boundary == "lost_receipt" else 0)
            assert not core.store.rows("SELECT * FROM model_usage")
    run(scenario())


@pytest.mark.parametrize("boundary", ["stop", "expired", "wrong_source",
                                     "source_replaced", "recall"])
def test_native_upload_rechecks_original_source_and_lease(core, tmp_path, boundary):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, image = await admit_image(core, client, config, plan, tmp_path)
            lease = await claim(client, event, image)
            body = pixels()
            if boundary == "stop":
                owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                    operation_id="stop-native-image", authority=plan.authority))
            elif boundary == "expired":
                core.clock.advance(61)
            elif boundary == "source_replaced":
                row = core.store.one("SELECT body_json FROM events WHERE event_id=?",
                                     (event["event_id"],))
                changed = json.loads(row["body_json"])
                changed["rich"]["segments"][0]["source_sha256"] = "f" * 64
                core.store.execute("UPDATE events SET body_json=? WHERE event_id=?",
                                   (encode(changed), event["event_id"]))
            elif boundary == "recall":
                from companion.owner_authority_protocol import OwnerCapture
                from tests.support.owner_recall import receipt

                saved = json.loads(core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                    ("owner_input:" + event["event_id"],))["value"])
                capture = OwnerCapture.model_validate(saved["capture"])
                withdrawn = await client.post("/internal/v1/owner-authority/inputs/recall",
                    json=receipt(core, capture, plan.key).model_dump(mode="json"))
                assert withdrawn.status_code == 200, withdrawn.text
            else:
                image = {**image, "source_sha256": "f" * 64}
            checked = await client.post("/internal/v2/content/" + event["event_id"]
                + "/image/check", json={"lease_token": lease["lease_token"],
                                       "source_sha256": image["source_sha256"]})
            assert checked.status_code in {403, 409, 410}, checked.text
            response = await client.post("/internal/v2/content/" + event["event_id"] + "/image",
                content=body, headers=upload_headers(plan, event, image, lease, body))
            assert response.status_code in {403, 409, 410}, response.text
            assert not core.store.one("SELECT 1 FROM captured_images")
            assert not core.store.one("SELECT 1 FROM model_usage")
            assert not core.store.one("SELECT 1 FROM outbox")
    run(scenario())


def test_superseding_text_reuses_the_original_image_lease_and_bytes(core, tmp_path):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True, history=True) as values:
            client, config, plan = values
            first, image = await admit_image(core, client, config, plan, tmp_path)
            lease = await claim(client, first, image)
            frozen = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
            latest, _ = await admit_image(core, client, config, plan, tmp_path, number=43,
                message=[{"type": "text", "data": {"text": "请结合刚才的原图。"}}])
            assert core.claim_job("wait-for-old-image") is None
            repeated = await client.post(
                "/internal/v2/content/" + first["event_id"] + "/image/claim",
                json={"request_key": "image:0", "source_sha256": image["source_sha256"]})
            assert repeated.status_code == 409
            body = pixels()
            uploaded = await client.post("/internal/v2/content/" + first["event_id"] + "/image",
                content=body, headers=upload_headers(plan, first, image, lease, body))
            assert uploaded.status_code == 200, uploaded.text
            row = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
            assert {k: row[k] for k in ("lease_token", "lease_until", "reserved_bytes")} == {
                k: frozen[k] for k in ("lease_token", "lease_until", "reserved_bytes")}
            provider = Provider(answer("结合原图回答。"))
            async with worker_for(core, provider) as worker:
                assert await worker.run_once("latest-image-consumer")
            status = core.store.one("SELECT state,last_error_code FROM jobs WHERE event_id=?",
                                    (latest["event_id"],))
            assert status["state"] == "SUCCEEDED", status
            assert len(provider.requests) == 1
            messages = provider.requests[0]["messages"]
            assert any(isinstance(row["content"], list) for row in messages[:-1])
            assert messages[-1]["content"] == "请结合刚才的原图。"
            assert core.store.rows("SELECT origin_event_id FROM outbox") == [
                {"origin_event_id": latest["event_id"]}]
            assert len(core.store.rows("SELECT * FROM captured_images")) == 1
    run(scenario())


def test_unknown_image_model_call_is_not_replayed_after_restart(core, tmp_path):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, image = await admit_image(core, client, config, plan, tmp_path)
            lease = await claim(client, event, image)
            body = pixels()
            result = await client.post("/internal/v2/content/" + event["event_id"] + "/image",
                content=body, headers=upload_headers(plan, event, image, lease, body))
            assert result.status_code == 200, result.text
            calls = []

            def uncertain(request):
                calls.append(request)
                raise httpx.ReadTimeout("synthetic lost model receipt", request=request)

            async with worker_for(core, uncertain) as worker:
                assert await worker.run_once("unknown-image-call")
            before = core.store.rows("SELECT * FROM model_usage")
            assert len(before) == 1 and before[0]["state"] == "UNKNOWN"
            assert before[0]["billed_micro_usd"] > 0
            core.clock.advance(core.settings.lease_seconds + 1)
            async with worker_for(core, uncertain) as restarted:
                await restarted.run_once("never-replay-image-call")
            assert len(calls) == 1
            assert core.store.rows("SELECT * FROM model_usage") == before
            assert_failure_notice(core)
            failed = core.store.one("SELECT job_id,last_error_code FROM jobs WHERE event_id=?",
                                     (event["event_id"],))
            receipts = core.store.rows("SELECT safe_detail_json FROM audit_log "
                "WHERE action='job.failed' AND entity_id=? ORDER BY rowid", (failed["job_id"],))
            assert [json.loads(row["safe_detail_json"]) for row in receipts] == [
                {"code": "MODEL_RETRYABLE", "state": "RETRY_WAIT"},
                {"code": "CONVERSATION_OUTCOME_UNAVAILABLE", "state": "FAILED"}]
            assert failed["last_error_code"] == "CONVERSATION_OUTCOME_UNAVAILABLE"
    run(scenario())


def test_native_recall_erases_uploaded_original_and_rejects_replay(core, tmp_path):
    from companion.owner_authority_protocol import OwnerCapture
    from tests.support.owner_recall import receipt

    async def scenario():
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, image = await admit_image(core, client, config, plan, tmp_path)
            lease = await claim(client, event, image)
            body = pixels()
            url = "/internal/v2/content/" + event["event_id"] + "/image"
            headers = upload_headers(plan, event, image, lease, body)
            response = await client.post(url, content=body, headers=headers)
            assert response.status_code == 200, response.text
            row = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
            path = image_spool.directory(core.store) / (row["item_id"] + ".raw")
            assert path.exists()
            saved = json.loads(core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                ("owner_input:" + event["event_id"],))["value"])
            notice = receipt(core, OwnerCapture.model_validate(saved["capture"]), plan.key)
            withdrawn = await client.post("/internal/v1/owner-authority/inputs/recall",
                json=notice.model_dump(mode="json"))
            assert withdrawn.status_code == 200, withdrawn.text
            assert not path.exists()
            assert not core.store.one("SELECT 1 FROM captured_images")
            replay = await client.post(url, content=body, headers=headers)
            assert replay.status_code in {403, 410}
            assert not core.store.one("SELECT 1 FROM model_usage")
            assert not core.store.one("SELECT 1 FROM outbox")
    run(scenario())
