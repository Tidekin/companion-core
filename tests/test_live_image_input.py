"""Authenticated native image capture, original uploads and SDK-only paid inference."""

import io
import json

import httpx
import pytest
from PIL import Image

from companion import budget, owner_authority, owner_input, paid_budget
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from companion.store import encode
from tests.conftest import run
from tests.support.failure_notice import assert_failure_notice
from tests.support.live_image_input import image_parts, options, png, resolve
from tests.support.native_images import image_host, raw_event, upload
from tests.support.onebot_images import image_segment


def test_authenticated_image_upload_defers_paid_inference_to_original_sdk_job(core, tmp_path):
    calls, downloads = [], []

    async def source(ws):
        await ws.send(json.dumps(raw_event(message=[
            {"type": "text", "data": {"text": "看看这张图"}}, image_segment()])))
        await ws.wait_closed()

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "我看到蓝色。"}}],
                                       "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    def cdn(request):
        downloads.append(request)
        return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})

    async def scenario():
        async with image_host(core, tmp_path, source, options=options(), model_reply=provider) as (
            active, _, sends, _,
        ):
            await active.capture()
            assert not core.store.one("SELECT 1 FROM events")
            await active.flush()
            job = core.store.one("SELECT * FROM jobs")
            assert not core.claim_job("wait-for-original", job_id=job["job_id"])
            await upload(active, transport=httpx.MockTransport(cdn), resolver=resolve)
            assert len(downloads) == 1 and calls == []
            assert core.store.one("SELECT state FROM content_items")["state"] == "SUCCEEDED"
            assert not core.store.rows("SELECT * FROM model_usage")
            assert await core.run_once()
            assert len(calls) == 1
            assert len(image_parts(calls[0])) == 1
            assert image_parts(calls[0])[0]["image_url"]["url"].startswith("data:image/png;")
            assert core.store.rows("SELECT scenario,state FROM model_usage") == [
                {"scenario": "chat", "state": "SETTLED"}]
            assert core.store.one("SELECT logical_request_id FROM model_admissions")[
                "logical_request_id"] == job["job_id"]
            assert await active.pump()
            assert active.attempts() == {"ACK": 1} and len(sends) == 1
            assert paid_budget.journal(core.store, core.settings.paid_model)["head_sha256"]
            assert not core.store.rows("SELECT * FROM observations")
    run(scenario())


@pytest.mark.parametrize("second", ["text", "image"])
def test_current_image_scope_has_no_implicit_history_or_extra_paid_call(core, tmp_path, second):
    number, calls, downloads = 0, [], []

    async def source(ws):
        nonlocal number
        number += 1
        message = ([{"type": "text", "data": {"text": "当前纯文字"}}]
                   if number == 2 and second == "text" else [image_segment()])
        await ws.send(json.dumps(raw_event(number, message=message)))
        await ws.wait_closed()

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "合成回复"}}],
                                       "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    def cdn(request):
        downloads.append(request)
        return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})

    async def scenario():
        async with image_host(core, tmp_path, source, options=options(), model_reply=provider) as (
            active, _, sends, _,
        ):
            for expected in (1, 2):
                await active.capture(expected)
                await active.flush()
                await upload(active, transport=httpx.MockTransport(cdn), resolver=resolve)
                assert await core.run_once()
                assert await active.pump()
            assert len(calls) == len(sends) == 2
            assert len(downloads) == (1 if second == "text" else 2)
            assert len(image_parts(calls[-1])) == int(second == "image")
            assert len(core.store.rows("SELECT * FROM model_admissions")) == 2
            assert not core.store.rows("SELECT * FROM observations")
    run(scenario())


@pytest.mark.parametrize("failure", [
    "too_many_images", "mime", "pixels", "dns_stop", "unknown_usage",
    "source_after_dispatch", "media_after_dispatch", "reserve", "signature", "ledger",
    "upload_bytes", "upload_mime", "upload_lease", "lease_before_upload",
])
def test_native_image_intake_failure_retains_source_and_one_shot_accounting(
    core, tmp_path, monkeypatch, failure,
):
    calls, downloads = [], []

    async def source(ws):
        await ws.send(json.dumps(raw_event(message=[
            {"type": "text", "data": {"text": "看图"}},
            *([image_segment()] * (5 if failure == "too_many_images" else 1))])))
        await ws.wait_closed()

    def provider(request):
        calls.append(request)
        if failure == "media_after_dispatch":
            core.store.execute('UPDATE content_items SET media_sha256=%s', ("0" * 64,))
        if failure == "source_after_dispatch":
            source_id = core.store.one("SELECT event_id FROM events WHERE source='qq.onebot'")[
                "event_id"]
            with core.store.transaction():
                core.ledger.expire_events(core.store, [source_id], core.clock())
        value = {"choices": [{"message": {"content": "合成图片分析"}}]}
        if failure != "unknown_usage":
            value["usage"] = {"prompt_tokens": 100, "completion_tokens": 10}
        return httpx.Response(200, json=value)

    def cdn(request):
        downloads.append(request)
        return httpx.Response(200, content=png(), headers={
            "Content-Type": "text/plain" if failure == "mime" else "image/png"})

    async def dns(host, port):
        if failure == "dns_stop":
            current = owner_authority.current(core.store, core.settings, core.clock)
            owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                operation_id="stop-image-dns", authority=current.authority.as_dict()))
        return ["1.1.1.1"]

    opts = options()
    if failure == "pixels":
        opts["paid_options"]["image_input"] = opts["paid_options"]["image_input"].model_copy(
            update={"max_pixels_per_image": 1})

    async def scenario():
        async with image_host(core, tmp_path, source, options=opts, model_reply=provider) as (
            active, _, sends, _,
        ):
            await active.capture()
            if failure == "too_many_images":
                with pytest.raises(httpx.HTTPStatusError):
                    await active.flush()
                assert not core.store.rows("SELECT * FROM events")
                return
            await active.flush()
            event_id = core.store.one("SELECT event_id FROM events")["event_id"]
            if failure == "signature":
                key = owner_input.INPUT + event_id
                value = json.loads(core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                                                  (key,))["value"])
                value["capture"]["signature"] = "0" * 128
                core.store.execute('UPDATE runtime_meta SET value=%s WHERE key=%s',
                                   (encode(value), key))
            elif failure == "ledger":
                def crash(*args):
                    raise RuntimeError("synthetic apply failure")
                with monkeypatch.context() as patch:
                    patch.setattr(core.ledger, "apply", crash)
                    with pytest.raises(RuntimeError):
                        with core.store.transaction():
                            core.ledger.expire_events(core.store, [event_id], core.clock())

            async def mutate_upload(request):
                if not request.url.path.endswith("/image"):
                    return
                if failure == "upload_bytes":
                    changed = io.BytesIO()
                    Image.new("RGB", (2, 2), "red").save(changed, format="PNG")
                    request._content = changed.getvalue()
                    request.stream = httpx.ByteStream(request._content)
                    request.headers["Content-Length"] = str(len(request._content))
                elif failure == "upload_mime":
                    request.headers["Content-Type"] = "image/jpeg"
                elif failure == "upload_lease":
                    proof = json.loads(request.headers["X-Content-Proof"])
                    proof["lease_token"] = "content_lease_" + "0" * 32
                    request.headers["X-Content-Proof"] = encode(proof)
                elif failure == "lease_before_upload":
                    core.store.execute('UPDATE content_items SET lease_token=%s',
                                       ("content_lease_" + "0" * 32,))
            active.client.event_hooks["request"].append(mutate_upload)
            model_cases = {"unknown_usage", "source_after_dispatch", "media_after_dispatch",
                           "reserve"}
            if failure in model_cases:
                await upload(active, transport=httpx.MockTransport(cdn), resolver=dns)
                if failure == "reserve":
                    def refused(*args, **kwargs):
                        raise CoreError("SYNTHETIC_RESERVE_FAILED", 429)
                    monkeypatch.setattr(budget, "reserve", refused)
                assert await core.run_once()
            elif failure == "dns_stop":
                with pytest.raises(httpx.HTTPStatusError) as stopped:
                    await upload(active, transport=httpx.MockTransport(cdn), resolver=dns)
                assert stopped.value.response.status_code == 403
                state, manifest = active.runtime.db.execute(
                    "SELECT state,manifest_json FROM automatic_media").fetchone()
                assert state == "DOWNLOADING" and manifest is not None
            else:
                with pytest.raises((ValueError, httpx.HTTPStatusError, CoreError)):
                    await upload(active, transport=httpx.MockTransport(cdn), resolver=dns)
            assert not sends and len(calls) <= 1
            if failure in {"media_after_dispatch", "reserve"}:
                assert_failure_notice(core)
            else:
                assert not core.store.rows("SELECT * FROM outbox")
            assert not core.store.rows("SELECT * FROM observations")
            if failure in {"dns_stop", "signature", "ledger"}:
                assert not downloads and not calls
            else:
                assert len(downloads) == 1
            if failure in {"source_after_dispatch", "media_after_dispatch"}:
                assert len(calls) == 1
                assert core.store.one(
                    "SELECT state,input_tokens,output_tokens FROM model_usage") == {
                    "state": "SETTLED", "input_tokens": 100, "output_tokens": 10}
            elif failure == "unknown_usage":
                assert len(calls) == 1
                assert core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"
            else:
                assert not calls and not core.store.rows("SELECT * FROM model_usage")
    run(scenario())
