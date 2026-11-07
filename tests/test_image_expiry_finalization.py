"""Already dispatched image replies can finish after the original spool expires."""

import base64
import json
from datetime import datetime, timedelta

import httpx
import pytest

from companion import image_spool
from companion.agent_persistence import ConversationStore
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, call, worker_for
from tests.support.failure_notice import assert_failure_notice
from tests.support.onebot import provider
from tests.support.prepare_owner_input import native_spool, window
from tests.test_native_image_pipeline import admit_image, claim, pixels, upload_headers


@pytest.mark.parametrize("boundary", [
    "model_response", "save_output", "restart", "changed_proof", "next_request",
])
def test_text_reply_finishes_when_historical_image_expires(core, tmp_path, monkeypatch, boundary):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True, history=True) as values:
            client, config, grant = values
            first, image = await admit_image(core, client, config, grant, tmp_path)
            lease = await claim(client, first, image)
            data = pixels()
            uploaded = await client.post(
                "/internal/v2/content/" + first["event_id"] + "/image", content=data,
                headers=upload_headers(grant, first, image, lease, data))
            assert uploaded.status_code == 200, uploaded.text
            expiry = core.store.one("SELECT expires_at FROM captured_images")["expires_at"]
            core.clock.now = datetime.fromisoformat(expiry) - timedelta(seconds=1)
            latest, _ = await admit_image(core, client, config, grant, tmp_path, number=43,
                message=[{"type": "text", "data": {"text": "继续回答刚才的问题。"}}])

            def expire():
                core.clock.advance(2)
                image_spool.cleanup(core.store, core.clock(), retain_completed=True)
                assert not list(image_spool.directory(core.store).glob("*.raw"))

            model = Provider(answer(calls=[call("clock", "current_time", {})])
                             if boundary == "next_request" else answer("这是文字问题的完整回答。"))

            def respond(request):
                response = model(request)
                if boundary in {"model_response", "changed_proof", "next_request"}:
                    expire()
                if boundary == "changed_proof":
                    proof = json.loads(core.store.one("SELECT proof_json FROM captured_images")[
                        "proof_json"])
                    proof["signature"] = "0" * 128
                    core.store.execute("UPDATE captured_images SET proof_json=?",
                                       (json.dumps(proof),))
                return response

            class SimulatedExit(BaseException):
                pass

            if boundary in {"save_output", "restart"}:
                original = ConversationStore.save_result

                def save_result(persistence, result):
                    if boundary == "restart":
                        original(persistence, result)
                        expire()
                        raise SimulatedExit()
                    expire()
                    return original(persistence, result)

                monkeypatch.setattr(ConversationStore, "save_result", save_result)
            async with worker_for(core, respond) as worker:
                if boundary == "restart":
                    with pytest.raises(SimulatedExit):
                        await worker.run_once("historical-image-expiry")
                else:
                    assert await worker.run_once("historical-image-expiry")
            if boundary == "restart":
                core.clock.advance(core.settings.lease_seconds + 1)
                async with worker_for(core, model) as worker:
                    assert await worker.run_once("resume-frozen-image-reply")
            job = core.store.one("SELECT * FROM jobs WHERE event_id=?", (latest["event_id"],))
            if boundary in {"changed_proof", "next_request"}:
                assert job["state"] == "FAILED", job["last_error_code"]
                assert len(model.requests) == 1
                assert_failure_notice(core, latest["event_id"])
                if boundary == "next_request":
                    assert job["last_error_code"] == "LIVE_IMAGE_ORIGINAL_UNAVAILABLE"
                return
            assert job["state"] == "SUCCEEDED", job["last_error_code"]
            rows = core.store.rows("SELECT content_json FROM outbox WHERE origin_event_id=?",
                                   (latest["event_id"],))
            assert len(rows) == 1 and "完整回答" in rows[0]["content_json"]
            assert len(model.requests) == 1
            usage = core.store.rows("SELECT state FROM model_usage WHERE job_id=?",
                                    (job["job_id"],))
            assert usage == [{"state": "SETTLED"}]
            saved = core.store.one("SELECT * FROM agent_conversations WHERE job_id=?",
                                   (job["job_id"],))
            assert saved["phase"] == "COMPLETE"
            assert "companion-image:" in saved["conversation_json"]
            assert base64.b64encode(data).decode() not in saved["conversation_json"]
            spool, runtime = native_spool(core, tmp_path, config, grant)
            sends = []
            try:
                async with private_http_client(config.onebot_http_url, config.onebot_token,
                    transport=httpx.MockTransport(lambda request: provider(request, sends)),
                ) as protocol:
                    guard = OwnerSendGuard(runtime, client)
                    await deliver_tick(client, OwnerOneBot(guard, protocol), spool, guard=guard)
                assert len(sends) == 1
                assert core.store.one("SELECT state FROM outbox WHERE origin_event_id=?",
                                      (latest["event_id"],)) == {"state": "SENT"}
            finally:
                spool.close()
    run(scenario())
