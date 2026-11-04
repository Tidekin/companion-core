"""Background original-media work yields admission and keeps one-shot I/O after restart."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from companion_onebot_bridge import app, pending_media
from tests.conftest import run
from tests.native_continuous_support import frame, host, recall_frame, until
from tests.support.onebot_events import raw_event
from tests.test_input_interrupt_bridge import model_response
from tests.test_live_image_input import options as image_options
from tests.test_live_image_input import png
from tests.test_onebot_images import image_segment
from tests.test_private_companion import companion_options


def scope(core):
    options, images = companion_options(core), image_options()
    options.update(image_scope=images["image_scope"],
                   image_approval_sha256=images["image_approval_sha256"])
    options["core_options"].update(images["core_options"])
    options["paid_options"]["image_input"] = images["paid_options"]["image_input"]
    return options


def original():
    return raw_event(1, message=[{"type": "text", "data": {"text": "看看这张图片"}},
                                 image_segment()])


async def resolve(*_):
    return ["1.1.1.1"]


async def advance_clock(core, stop):
    initial, start = core.clock.now, time.monotonic()
    while not stop.is_set():
        core.clock.now = initial + timedelta(seconds=time.monotonic() - start)
        await asyncio.sleep(0.01)


def media_row(window):
    return pending_media._execute(window.runtime, "SELECT * FROM automatic_media").fetchone()


@asynccontextmanager
async def media_host(core, tmp_path, source, *, model_reply=None, independent_api=False):
    options = scope(core)
    options["core_options"]["rich_input_scope"] = "references_v1"
    async with host(core, tmp_path, source, model_reply=model_reply,
        purposes=("chat", "image", "content", "history"), core_options=options["core_options"],
        paid_options=image_options()["paid_options"],
        bridge_options={"rich_input_scope": "references_v1"},
        independent_api=independent_api) as values:
        yield values


@pytest.mark.parametrize("phase", ["download", "upload"])
@pytest.mark.parametrize("control", ["new_input", "recall"])
def test_media_wait_does_not_block_new_input_or_recall(core, tmp_path, monkeypatch, phase, control):
    from companion import api
    from companion.core import Core
    from tests.test_owner_image_recovery import run_worker

    monkeypatch.setattr(api, "Core", lambda *args, **kwargs: Core(
        *args, clock=core.clock, **kwargs))
    started, release = asyncio.Event(), asyncio.Event()
    downloads, calls = [], []

    async def source(ws):
        await ws.send(json.dumps(original() | {"time": int(core.clock.now.timestamp())}))
        await asyncio.wait_for(started.wait(), 15)
        await ws.send(recall_frame() if control == "recall" else frame(
            2, text="请结合刚才的图片继续说明"))
        await ws.wait_closed()

    async def cdn(request):
        downloads.append(request)
        if phase == "download":
            started.set()
            await asyncio.wait_for(release.wait(), 15)
        return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})

    async def answer(request):
        calls.append(json.loads(request.content)["messages"])
        return model_response("好，结合刚才的原图继续说明。")

    async def scenario():
        async with media_host(core, tmp_path, source, model_reply=answer, independent_api=True) as (
            window, _, sends, _,
        ):
            # Signed captures, recall frames and Core admission share this fixture clock.
            from tests import native_continuous_support

            receipt_clock = SimpleNamespace(now=lambda _: core.clock.now)
            monkeypatch.setattr(app, "datetime", receipt_clock)
            monkeypatch.setattr(native_continuous_support, "datetime", receipt_clock)

            async def accepted(response):
                path = response.request.url.path
                if response.is_success and (
                    (control == "recall" and path.endswith("/inputs/recall"))
                    or (control == "new_input" and path == "/internal/v1/inbox/events"
                        and json.loads(response.request.content)["payload"]["text"]
                        == "请结合刚才的图片继续说明")
                ):
                    assert started.is_set() and not release.is_set()
                    release.set()

            async def uploading(request):
                if phase == "upload" and request.url.path.endswith("/image"):
                    started.set()
                    await asyncio.wait_for(release.wait(), 15)

            window.client.event_hooks["response"].append(accepted)
            window.client.event_hooks["request"].append(uploading)
            pump = pending_media.MediaPump(window.runtime, window.client,
                image_transport=httpx.MockTransport(cdn), image_resolver=resolve)
            window.start(worker=False)
            task = asyncio.create_task(app.transfer_media(pump, window.stop))
            try:
                try:
                    await until(lambda: release.is_set() and window.db.execute(
                        "SELECT state FROM automatic_media").fetchone()[0] in {"DONE", "CANCELLED"},
                        tasks=[task, *window.tasks], deadline_seconds=15)
                except TimeoutError as error:
                    raise AssertionError(json.dumps({"released": release.is_set(),
                        "downloads": len(downloads), "calls": len(calls), "sends": len(sends),
                        "media": window.db.execute(
                            "SELECT state,next_slot FROM automatic_media").fetchall(),
                        "jobs": core.store.rows("SELECT kind,state,last_error_code FROM jobs"),
                        "content": core.store.rows(
                            "SELECT kind,state FROM content_items")})) from error
                if control == "new_input":
                    requests = await run_worker(core, tmp_path, 1, "结合原图继续说明。")
                    calls.extend(request["messages"] for request in requests)
                    await until(lambda: len(sends) == 1, tasks=[task, *window.tasks],
                                deadline_seconds=15)
                assert window.runtime.row()["state"] == "ACTIVE"
                assert len(downloads) == 1
                # Native upload stores bytes; only a surviving SDK chat invokes the model.
                assert len(calls) == int(control == "new_input")
                media = media_row(window)
                assert media["state"] == ("DONE" if control == "new_input" else "CANCELLED")
                assert media["manifest_json"] is None
                if control == "new_input":
                    assert any(part["type"] == "image_url" for message in calls[0]
                               if isinstance(message["content"], list)
                               for part in message["content"])
                assert not core.store.one("SELECT 1 FROM model_usage WHERE state!='SETTLED'")
                window.runtime.verify()
            finally:
                release.set()
                started.set()
                await window.pause()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await pump.close()
    run(scenario())


@pytest.mark.parametrize("resume", ["queued", "upload_completed"])
def test_restart_recovers_queued_or_confirmed_upload_without_repeating_io(
    core, tmp_path, monkeypatch, resume,
):
    downloads, calls = [], []

    async def source(ws):
        await ws.send(json.dumps(original() | {"time": int(datetime.now(UTC).timestamp())}))
        await ws.wait_closed()

    def cdn(request):
        downloads.append(request)
        return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})

    def answer(request):
        calls.append(request)
        return model_response("蓝色图片" if len(calls) == 1 else "我看到了蓝色图片。")

    async def scenario():
        async with media_host(core, tmp_path, source, model_reply=answer) as (
            window, _, sends, _,
        ):
            client = window.client
            await window.capture()
            await window.flush()
            queued = media_row(window)
            assert queued["state"] == "PENDING" and "看看这张图片" not in queued["manifest_json"]
            if resume == "upload_completed":
                transition = pending_media.transition

                def crash(window, event_id, state, slot, receipt=None):
                    if state == "DONE":
                        raise RuntimeError("synthetic crash before completion receipt")
                    return transition(window, event_id, state, slot, receipt)

                monkeypatch.setattr(pending_media, "transition", crash)
                pump = pending_media.MediaPump(window.runtime, client,
                    image_transport=httpx.MockTransport(cdn), image_resolver=resolve)
                with pytest.raises(RuntimeError, match="synthetic crash"):
                    await pump.execute(queued)
                monkeypatch.setattr(pending_media, "transition", transition)
                assert window.db.execute("SELECT state FROM automatic_media").fetchone()[0] == (
                    "UPLOADING")
            window.restart()
            pump = pending_media.MediaPump(window.runtime, client,
                image_transport=httpx.MockTransport(cdn), image_resolver=resolve)
            await pump.execute(media_row(window))
            assert await core.run_once()
            assert await window.pump()
            assert window.runtime.row()["state"] == "ACTIVE"
            assert window.attempts() == {"ACK": 1}
            assert len(downloads) == len(calls) == len(sends) == 1
            media = media_row(window)
            assert media["state"] == "DONE" and media["manifest_json"] is None
            window.runtime.verify()

    run(scenario())


@pytest.mark.parametrize("recovery", ["active", "expired", "erased"])
def test_restart_download_waits_original_lease_without_replaying_get(core, tmp_path, recovery):
    downloads, claims, checks = [], [], []

    async def source(ws):
        await ws.send(json.dumps(original() | {"time": int(datetime.now(UTC).timestamp())}))
        await ws.wait_closed()

    def cdn(request):
        downloads.append(request)
        raise RuntimeError("synthetic crash after original GET")

    async def scenario():
        async with media_host(core, tmp_path, source) as (window, _, sends, _):
            client = window.client

            async def track(request):
                if request.url.path.endswith("/image/claim"):
                    claims.append(json.loads(request.content))
                elif request.url.path.endswith("/image/check"):
                    checks.append(json.loads(request.content))

            client.event_hooks["request"].append(track)
            await window.capture()
            await window.flush()
            row = media_row(window)
            first = pending_media.MediaPump(window.runtime, client,
                image_transport=httpx.MockTransport(cdn), image_resolver=resolve)
            with pytest.raises(RuntimeError, match="synthetic crash"):
                await first.execute(row)
            row = media_row(window)
            assert row["state"] == "DOWNLOADING"
            original_claim = json.loads(row["receipt_json"])
            original_source = json.loads(row["manifest_json"])["images"][0]["source_sha256"]
            item = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
            if recovery == "expired":
                core.clock.advance(61)
                await core.content.run_once()
            elif recovery == "erased":
                with core.store.transaction():
                    core.ledger.expire_events(core.store, [row["event_id"]], core.clock())
            window.restart()
            resumed = pending_media.MediaPump(window.runtime, client,
                image_transport=httpx.MockTransport(cdn), image_resolver=resolve)
            if recovery == "active":
                task = asyncio.create_task(resumed.execute(row))
                try:
                    await asyncio.sleep(0.1)
                    assert not task.done() and len(downloads) == 1
                    core.clock.advance(61)
                    await core.content.run_once()
                    await asyncio.wait_for(task, 5)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            else:
                await resumed.execute(row)
            assert len(downloads) == 1 and sends == []
            assert core.store.rows("SELECT * FROM model_usage") == []
            assert claims == [{"request_key": "image:0", "source_sha256": original_source}]
            assert checks and all(check == {"lease_token": original_claim["lease_token"],
                "source_sha256": original_source} for check in checks)
            current = core.store.one("SELECT * FROM content_items WHERE item_id=?",
                                     (item["item_id"],))
            assert current["reserved_bytes"] == item["reserved_bytes"]
            if recovery != "erased":
                assert current["state"] == "FAILED"
                assert current["lease_token"] == original_claim["lease_token"]
                assert current["lease_until"] == original_claim["lease_until"]
                retry = media_row(window)
                assert retry["state"] == "DONE"
            if recovery == "erased":
                cancelled = media_row(window)
                assert cancelled["state"] == "CANCELLED" and cancelled["manifest_json"] is None
            window.runtime.stop()
            row = media_row(window)
            assert row["state"] == ("CANCELLED" if recovery == "erased" else "DONE")
            assert row["manifest_json"] is None
            before = len(downloads), len(claims), len(checks)
            with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                window.runtime.verify()
            await resumed.execute(row)
            assert (len(downloads), len(claims), len(checks)) == before
            assert core.store.rows("SELECT * FROM model_usage") == [] and sends == []
            window.runtime.verify(active=False)

    run(scenario())


@pytest.mark.parametrize("failure", ["authority_denied", "receipt_changed"])
def test_unproven_native_media_failure_keeps_original_pending_custody(core, tmp_path, failure):
    from companion.owner_authority_protocol import OwnerRuntimeConfig

    async def source(ws):
        await ws.send(json.dumps(original() | {"time": int(datetime.now(UTC).timestamp())}))
        await ws.wait_closed()

    async def scenario():
        async with media_host(core, tmp_path, source) as (window, _, sends, calls):
            await window.capture()
            await window.flush()
            before = dict(media_row(window))
            traffic = []

            async def track(request):
                traffic.append(request.url.path)

            async def changed(response):
                if response.request.url.path.endswith("/inputs/state"):
                    await response.aread()
                    response._content = json.dumps(response.json() | {
                        "input_sha256": "0" * 64}).encode()

            window.client.event_hooks["request"].append(track)
            if failure == "authority_denied":
                core.settings.owner_runtime = OwnerRuntimeConfig(mode="OFF")
            else:
                window.client.event_hooks["response"].append(changed)
            pump = pending_media.MediaPump(window.runtime, window.client)
            if failure == "authority_denied":
                with pytest.raises(httpx.HTTPStatusError) as error:
                    await pump.execute(media_row(window))
                assert error.value.response.status_code == 403
            else:
                with pytest.raises(ValueError, match="OWNER_INPUT_STATE_INVALID"):
                    await pump.execute(media_row(window))
            assert dict(media_row(window)) == before
            assert not any(path.endswith(("/image", "/image/claim")) for path in traffic)
            assert sends == calls == [] and core.store.rows("SELECT * FROM model_usage") == []
            window.runtime.verify()
    run(scenario())


def test_media_source_erasure_keeps_original_unknown_model_reservation(core, tmp_path, monkeypatch):
    calls, downloads = [], []

    async def source(ws):
        await ws.send(json.dumps(original() | {"time": int(datetime.now(UTC).timestamp())}))
        await ws.wait_closed()

    def answer(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic model result unknown", request=request)

    def download(request):
        downloads.append(request)
        return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})

    async def scenario():
        async with media_host(core, tmp_path, source, model_reply=answer) as (window, _, sends, _):
            await window.capture()
            await window.flush()
            transition = pending_media.transition

            def lose_completion(runtime, event_id, state, slot, receipt=None):
                if state == "DONE":
                    raise RuntimeError("synthetic completion receipt loss")
                return transition(runtime, event_id, state, slot, receipt)

            monkeypatch.setattr(pending_media, "transition", lose_completion)
            pump = pending_media.MediaPump(window.runtime, window.client,
                image_transport=httpx.MockTransport(download), image_resolver=resolve)
            with pytest.raises(RuntimeError, match="synthetic completion receipt loss"):
                await pump.execute(media_row(window))
            monkeypatch.setattr(pending_media, "transition", transition)
            assert media_row(window)["state"] == "UPLOADING"
            assert await core.run_once()
            assert len(calls) == len(downloads) == 1
            assert core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"
            with core.store.transaction():
                core.ledger.expire_events(core.store, [media_row(window)["event_id"]], core.clock())
            usage = core.store.rows("SELECT * FROM model_usage")
            await pump.execute(media_row(window))
            saved = media_row(window)
            assert saved["state"] == "CANCELLED" and saved["manifest_json"] is None
            receipt = json.loads(saved["receipt_json"])
            assert receipt["state"] == "RECALLED" and receipt["unsettled_model"]
            assert not receipt["terminal"]
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert len(calls) == len(downloads) == 1 and sends == []
            window.runtime.verify()
    run(scenario())


@pytest.mark.parametrize("origin", ["missing", "other"])
def test_original_input_custody_rejects_reparented_unknown_delivery(core, tmp_path, origin):
    number, delegated = 0, 0

    async def source(ws):
        nonlocal number
        number += 1
        await ws.send(frame(number, text=f"真实合成输入 {number}"))
        await ws.wait_closed()

    def remote(request):
        nonlocal delegated
        if request.url.path == "/send_private_msg":
            delegated += 1
            if delegated == 2:
                raise httpx.ReadTimeout("synthetic original delivery unknown", request=request)

    async def scenario():
        async with host(core, tmp_path, source, protocol_reply=remote) as (window, _, _, _):
            await window.capture()
            await window.flush()
            assert await core.run_once() and await window.pump()
            earlier = core.store.one("SELECT event_id FROM jobs WHERE kind='chat'")["event_id"]
            await window.capture(2)
            await window.flush()
            assert await core.run_once() and await window.pump()
            delivery = core.store.one("SELECT * FROM outbox WHERE state='DELIVERY_UNCERTAIN'")
            assert delivery and delegated == 2
            capture = json.loads(window.db.execute(
                "SELECT capture_json FROM window_inputs WHERE event_id=?",
                (delivery["origin_event_id"],)).fetchone()[0])
            path = "/internal/v1/owner-authority/inputs/state"
            response = await window.client.post(path, json=capture)
            assert response.status_code == 200, response.text
            assert response.json()["unsettled_delivery"] and not response.json()["terminal"]
            usage = core.store.rows("SELECT * FROM model_usage")
            core.store.execute("UPDATE outbox SET origin_event_id=? WHERE message_id=?",
                (None if origin == "missing" else earlier, delivery["message_id"]))
            before = core.store.rows("SELECT * FROM outbox")
            response = await window.client.post(path, json=capture)
            assert core.store.rows("SELECT * FROM outbox") == before
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert response.status_code == 409, response.text
            assert response.json() == {"error": "OWNER_DELIVERY_BINDING_REQUIRED"}
    run(scenario())
