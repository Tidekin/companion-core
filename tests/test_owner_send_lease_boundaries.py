"""Incident diagnostics use real custody/Core and mock only provider transport.

Clock jumps model elapsed guard work without changing leases or send semantics.
These are counterexamples and controls, not a claim about a production exception.
"""

import asyncio
import json
from datetime import datetime

import httpx
import pytest

from companion import agent_images
from companion_onebot_bridge import journal
from companion_onebot_bridge.capture import diagnostic_fields
from companion_onebot_bridge.images import OriginalImage, upload_original
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, worker_for
from tests.support.onebot import provider
from tests.test_native_image_pipeline import admit_image, pixels
from tests.test_prepare_owner_input import native_spool, window


@pytest.mark.parametrize("boundary", [
    "ack", "after_begin", "after_send_check", "after_io_started",
    "send_check_timeout", "lost_ack",
])
def test_delivery_failure_boundary_retains_actual_uncertainty(
    core, tmp_path, monkeypatch, boundary,
):
    async def scenario():
        async with window(core, tmp_path) as (_, config, plan), private_http_client(
            config.core_url, config.bridge_token,
        ) as client:
            await admit_image(core, client, config, plan, tmp_path, message=[
                {"type": "text", "data": {"text": "synthetic lease boundary"}}])
            model = Provider(answer("synthetic reply"))
            async with worker_for(core, model) as worker:
                assert await worker.run_once("lease-boundary-worker")
            usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
            assert len(model.requests) == len(usage) == 1
            assert usage[0]["state"] == "SETTLED"
            spool, runtime = native_spool(core, tmp_path, config, plan)
            sends, cancels, checks, failures = [], [], [], []
            guard = OwnerSendGuard(runtime, client)

            async def request_hook(request):
                if request.url.path.endswith("/send-check"):
                    checks.append(request)
                    if boundary == "send_check_timeout":
                        core.clock.advance(core.settings.lease_seconds + 1)
                        error = httpx.ReadTimeout("synthetic Core check timeout", request=request)
                        failures.append(error)
                        raise error

            async def response_hook(response):
                path = response.request.url.path
                if path.endswith("/cancel-before-send"):
                    await response.aread()
                    cancels.append((response.status_code, response.json()))
                if ((boundary == "after_begin" and path.endswith("/begin-send"))
                    or (boundary == "after_send_check" and path.endswith("/send-check"))):
                    assert response.status_code == 200
                    core.clock.advance(core.settings.lease_seconds + 1)

            append = runtime.append

            def after_append(kind, data):
                result = append(kind, data)
                if boundary == "after_io_started" and kind == "IO_STARTED":
                    core.clock.advance(core.settings.lease_seconds + 1)
                return result

            monkeypatch.setattr(runtime, "append", after_append)
            client.event_hooks["request"].append(request_hook)
            client.event_hooks["response"].append(response_hook)

            async def lose_ack(response):
                if boundary == "lost_ack" and response.request.url.path == "/send_private_msg":
                    error = httpx.ReadTimeout("synthetic ACK loss", request=response.request)
                    failures.append(error)
                    raise error

            try:
                async with private_http_client(config.onebot_http_url, config.onebot_token,
                    transport=httpx.MockTransport(lambda request: provider(request, sends)),
                ) as protocol:
                    protocol.event_hooks["response"].append(lose_ack)
                    assert await deliver_tick(client, OwnerOneBot(guard, protocol), spool,
                                              guard=guard)
                outbox = core.store.one("SELECT * FROM outbox")
                attempt = spool.db.execute("SELECT state FROM window_attempts").fetchone()[0]
                kinds = [record["kind"] for record in journal.read(runtime.path)[0]]
                result = spool.db.execute("SELECT body,state FROM results").fetchone()
                assert result[1] == "accepted"
                body = json.loads(result[0])
                assert outbox["attempt_count"] == 1
                assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
                if boundary == "ack":
                    assert len(sends) == 1 and guard.dispatched
                    assert attempt == "ACK" and outbox["state"] == "SENT"
                    assert body["result"] == "SENT" and body["provider_message_id"]
                    assert runtime.row()["state"] == "ACTIVE" and not runtime.stop_path.exists()
                    assert not cancels and kinds[-2:] == ["IO_STARTED", "ACK"]
                elif boundary == "send_check_timeout":
                    assert not sends and not guard.dispatched and not cancels
                    assert attempt == "NOT_SENT" and outbox["state"] == "CANCELLED"
                    assert body["result"] == "NOT_SENT"
                    assert outbox["last_error_code"] == "REPLY_NOT_DISPATCHED"
                    assert runtime.row()["state"] == "ACTIVE"
                    assert not runtime.stop_path.exists() and "IO_STARTED" not in kinds
                    assert len(failures) == 1
                else:
                    assert len(sends) == int(boundary == "lost_ack")
                    assert guard.dispatched == (boundary == "lost_ack")
                    assert attempt == "UNKNOWN" and outbox["state"] == "DELIVERY_UNCERTAIN"
                    assert body["result"] == "DELIVERY_UNCERTAIN"
                    assert body["error_code"] == outbox["last_error_code"] == (
                        "BRIDGE_SEND_INTERRUPTED")
                    assert not body.get("provider_message_id") and not outbox["sent_at"]
                    assert runtime.row()["state"] == "STOPPED" and runtime.stop_path.exists()
                    assert kinds[-2:] == ["UNKNOWN", "OWNER_STOPPED"] and "NOT_SENT" not in kinds
                    assert ("IO_STARTED" in kinds) == (boundary in {"after_io_started", "lost_ack"})
                    if boundary == "lost_ack":
                        assert not cancels
                    else:
                        assert len(cancels) == 1 and cancels[0][0] == 409
                        assert cancels[0][1]["error"] == "INPUT_CANCEL_BEFORE_SEND_UNPROVEN"
                    assert len(failures) == int(boundary in {"send_check_timeout", "lost_ack"})
                assert len(checks) == int(boundary != "after_begin")
                runtime.verify(active=False)
            finally:
                spool.close()

    run(scenario())


@pytest.mark.parametrize("boundary", ["ack", "guard_timeout", "lease_expiry"])
def test_image_guard_timeout_and_lease_expiry_are_distinct(core, tmp_path, boundary):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True) as (
            _, config, plan,
        ), private_http_client(config.core_url, config.bridge_token) as client:
            event, original = await admit_image(core, client, config, plan, tmp_path)
            spool, runtime = native_spool(core, tmp_path, config, plan)
            downloads, phases, claims, checks = [], [], [], []

            async def resolve(*_):
                return ["8.8.8.8"]

            async def download(request):
                downloads.append(request)
                return httpx.Response(200, content=pixels(), headers={"content-type": "image/png"})

            async def progress(state, receipt):
                phases.append(state)
                if state == "DOWNLOADING":
                    claims.append(receipt)
                    if boundary == "lease_expiry":
                        core.clock.advance(61)

            async def request_hook(request):
                if request.url.path.endswith("/image/check"):
                    checks.append(request)
                    if boundary == "guard_timeout":
                        # Exercise fetch_image's real 10 s deadline while its checked
                        # transport waits before the CDN delegate. No timeout is patched.
                        core.clock.advance(10)
                        await asyncio.Event().wait()

            client.event_hooks["request"].append(request_hook)
            try:
                async def upload():
                    return await upload_original(runtime, client, event["event_id"],
                        OriginalImage(**original), progress=progress, resolver=resolve,
                        transport=httpx.MockTransport(download))

                if boundary == "ack":
                    assert (await upload())["input_ready"]
                    assert len(downloads) == 1
                    assert phases == ["CLAIMING", "DOWNLOADING", "UPLOADING"]
                    assert core.store.one("SELECT COUNT(*) n FROM captured_images")["n"] == 1
                else:
                    expected = (TimeoutError if boundary == "guard_timeout"
                                else httpx.HTTPStatusError)
                    with pytest.raises(expected) as caught:
                        await upload()
                    assert not downloads and len(checks) == 1
                    assert phases == ["CLAIMING", "DOWNLOADING"] and len(claims) == 1
                    row = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
                    claim = claims[0]
                    assert row["state"] == "FETCHING" and row["reserved_bytes"] > 0
                    if boundary == "guard_timeout":
                        assert core.clock.now < datetime.fromisoformat(claim["lease_until"])
                        assert diagnostic_fields(caught.value) == {
                            "capture_phase": "core_check", "capture_reason": "CAPTURE_TIMEOUT"}
                        core.clock.advance(51)
                    else:
                        assert caught.value.response.status_code == 409
                    agent_images.expire_downloads(core.content)
                    expired = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
                    assert expired["state"] == "FAILED"
                    assert expired["error_code"] == "CONTENT_IMAGE_DOWNLOAD_EXPIRED"
                    assert expired["lease_token"] == claim["lease_token"] == row["lease_token"]
                    assert expired["lease_until"] == claim["lease_until"] == row["lease_until"]
                    assert expired["reserved_bytes"] == row["reserved_bytes"]
                    assert not core.store.one("SELECT 1 FROM captured_images")
                assert not core.store.one("SELECT 1 FROM model_usage")
                assert runtime.row()["state"] == "ACTIVE" and not runtime.stop_path.exists()
                runtime.verify()
            finally:
                spool.close()

    run(scenario())
