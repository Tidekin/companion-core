"""Original image evidence remains required after the SDK has produced a reply."""

import json
from datetime import datetime, timedelta

import httpx
import pytest

from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, worker_for
from tests.support.native_image_pipeline import admit_image, claim, pixels, upload_headers
from tests.support.onebot import provider
from tests.support.prepare_owner_input import native_spool, window


@pytest.mark.parametrize("drift,boundary", [
    ("none", "before_lease"),
    *[(drift, boundary) for drift in ("media", "proof", "snapshot", "expiry")
      for boundary in ("before_lease", "final_request")],
])
def test_native_reply_rechecks_original_image_after_generation(core, tmp_path, drift, boundary):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True) as (client, config, grant):
            event, image = await admit_image(core, client, config, grant, tmp_path)
            lease = await claim(client, event, image)
            data = pixels()
            accepted = await client.post(
                "/internal/v2/content/" + event["event_id"] + "/image", content=data,
                headers=upload_headers(grant, event, image, lease, data))
            assert accepted.status_code == 200, accepted.text
            model = Provider(answer("看到了蓝绿色的原图。"))
            async with worker_for(core, model) as worker:
                assert await worker.run_once("original-image-before-delivery")
            usage = core.store.rows("SELECT * FROM model_usage")
            assert len(model.requests) == len(usage) == 1
            assert usage[0]["state"] == "SETTLED"
            def change():
                if drift == "media":
                    core.store.execute('UPDATE content_items SET media_sha256=%s', ("0" * 64,))
                elif drift == "proof":
                    proof = json.loads(core.store.one(
                        "SELECT proof_json FROM captured_images")["proof_json"])
                    proof["signature"] = "0" * 128
                    core.store.execute('UPDATE captured_images SET proof_json=%s',
                                       (json.dumps(proof),))
                elif drift == "snapshot":
                    snapshots = json.loads(core.store.one(
                        "SELECT snapshots_json FROM agent_conversations")["snapshots_json"])
                    next(item for item in snapshots if item["kind"] == "images")["item_ids"] = []
                    core.store.execute('UPDATE agent_conversations SET snapshots_json=%s',
                                       (json.dumps(snapshots),))
                elif drift == "expiry":
                    expiry = core.store.one("SELECT expires_at FROM captured_images")["expires_at"]
                    core.clock.now = datetime.fromisoformat(expiry) + timedelta(seconds=1)

            if drift == "expiry" and boundary == "final_request":
                expiry = core.store.one("SELECT expires_at FROM captured_images")["expires_at"]
                # Cross the image deadline inside a still-valid delivery lease.
                core.clock.now = datetime.fromisoformat(expiry) - timedelta(seconds=1)
            if boundary == "before_lease":
                change()
            spool, runtime = native_spool(core, tmp_path, config, grant)
            sends = []
            try:
                async with private_http_client(config.onebot_http_url, config.onebot_token,
                    transport=httpx.MockTransport(lambda request: provider(request, sends)),
                ) as protocol:
                    async def final_request(request):
                        if request.url.path == "/send_private_msg":
                            change()

                    if boundary == "final_request":
                        protocol.event_hooks["request"].append(final_request)
                    guard = OwnerSendGuard(runtime, client)
                    await deliver_tick(client, OwnerOneBot(guard, protocol), spool, guard=guard)
                expected = drift in {"none", "expiry"}
                assert len(sends) == int(expected)
                assert bool(core.store.one("SELECT 1 FROM outbox WHERE state='SENT'")) == (
                    expected)
                assert core.store.rows("SELECT * FROM model_usage") == usage
            finally:
                spool.close()
    run(scenario())
