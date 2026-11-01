"""Signed HTTP animation intake and independent SDK dispatch, with provider HTTP mocked."""

import asyncio
import base64
import io
import json
import pickle
import sys
from pathlib import Path

import pytest
from PIL import Image

from companion import budget, owner_authority
from companion.owner_authority_protocol import AuthorityStop, OwnerCapture
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, worker_for
from tests.test_native_image_pipeline import admit_image, claim, upload_headers
from tests.test_owner_recall import receipt
from tests.test_prepare_owner_input import window
from tests.test_video_preview import FORMATS, animation


async def upload(core, client, config, plan, tmp_path, format, mime):
    event, image = await admit_image(core, client, config, plan, tmp_path)
    lease = await claim(client, event, image)
    original = animation(format)
    response = await client.post("/internal/v2/content/" + event["event_id"] + "/image",
        content=original, headers=upload_headers(plan, event, image, lease, original, mime))
    assert response.status_code == 200, response.text
    return event


async def recall(core, client, event, plan):
    saved = json.loads(core.store.one("SELECT value FROM runtime_meta WHERE key=?",
        ("owner_input:" + event["event_id"],))["value"])
    capture = OwnerCapture.model_validate(saved["capture"])
    response = await client.post("/internal/v1/owner-authority/inputs/recall",
        json=receipt(core, capture, plan.key).model_dump(mode="json"))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("format,mime", FORMATS)
def test_signed_animation_is_one_chat_call_with_later_frames_in_fresh_worker(
    core, tmp_path, format, mime,
):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True, video=True) as (
            client, config, plan,
        ):
            event = await upload(core, client, config, plan, tmp_path, format, mime)
            job = core.store.one("SELECT job_id FROM jobs WHERE event_id=?", (event["event_id"],))
            config_path, output = tmp_path / "preview-worker.pickle", tmp_path / "preview-wire.json"
            config_path.write_bytes(pickle.dumps((core.settings, core.clock.now, job["job_id"],
                [answer("Synthetic preview reply.")], "SUCCEEDED", str(output))))
            process = await asyncio.create_subprocess_exec(sys.executable, "-B",
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
            assert len(images) == 1 and "understand_video" in json.dumps(requests[0]["tools"])
            text = json.dumps(requests[0]["messages"], ensure_ascii=False)
            assert "第1,2,3,4帧" in text and "0,80,90,100" in text
            data = base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1])
            with Image.open(io.BytesIO(data)) as sheet:
                assert sheet.n_frames == 1 and max(sheet.size) <= 512
                assert sheet.getpixel((120, 12)) == (0, 255, 0)
                assert sheet.getpixel((24, 36)) == (0, 0, 255)
            assert core.store.rows("SELECT scenario,state FROM model_usage") == [
                {"scenario": "chat", "state": "SETTLED"}]
            assert core.store.one("SELECT COUNT(*) n FROM model_admissions")["n"] == 1
            assert core.store.one("SELECT COUNT(*) n FROM outbox WHERE state='PENDING'")["n"] == 1
            assert not core.store.one("SELECT 1 FROM agent_tool_calls")
            stored = core.store.one("SELECT conversation_json FROM agent_conversations")
            assert "data:image" not in stored["conversation_json"]
    run(scenario())


@pytest.mark.parametrize("boundary", ["stop", "recall", "budget"])
def test_preview_keeps_native_dispatch_fences_and_budget_responsibility(core, tmp_path, boundary):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True, video=True) as (
            client, config, plan,
        ):
            provider = Provider()
            reserved = None
            if boundary == "budget":
                other, _ = await admit_image(core, client, config, plan, tmp_path, number=41,
                    message=[{"type": "text", "data": {"text": "Synthetic concurrent source."}}])
                job = core.store.one("SELECT job_id FROM jobs WHERE event_id=?",
                                     (other["event_id"],))
                other_job = core.claim_job("preview-concurrent-reservation", job_id=job["job_id"])
                assert other_job
                reserved = budget.reserve(core.store, core.settings, job["job_id"], "chat",
                    [{"role": "user", "content": "Synthetic concurrent reservation."}],
                    core.clock(), clock=core.clock)
                core.fail_job(other_job, "MODEL_RESPONSE_INVALID")
            event = await upload(core, client, config, plan, tmp_path, "GIF", "image/gif")

            async def hook(request):
                if boundary == "stop":
                    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                        operation_id="stop-preview-dispatch", authority=plan.authority))
                elif boundary == "recall":
                    await recall(core, client, event, plan)

            async with worker_for(core, provider) as worker:
                worker.model.client.event_hooks["request"].append(hook)
                assert await worker.run_once("preview-native-boundary") is (boundary != "budget")
            # The concurrently failed job may carry Core's fixed failure notice only.
            assert not provider.requests and not core.store.one(
                "SELECT 1 FROM outbox WHERE delivery_key NOT LIKE 'failure:%'")
            usage = core.store.rows("SELECT state,billed_micro_usd FROM model_usage")
            if boundary == "budget":
                assert len(usage) == 1 and usage[0]["state"] == "RESERVED"
                assert core.store.one("SELECT state FROM model_usage WHERE request_id=?",
                                      (reserved,))["state"] == "RESERVED"
                waiting = core.store.one("SELECT state FROM jobs WHERE event_id=?",
                                         (event["event_id"],))
                assert waiting["state"] == "QUEUED"
                assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                                      (other_job["job_id"],))["state"] == "FAILED"
            else:
                assert usage == [{"state": "SETTLED", "billed_micro_usd": 0}]
            before = core.store.rows("SELECT * FROM model_usage")
            async with worker_for(core, provider) as restarted:
                await restarted.run_once("preview-no-replay")
            assert not provider.requests and core.store.rows("SELECT * FROM model_usage") == before
    run(scenario())
