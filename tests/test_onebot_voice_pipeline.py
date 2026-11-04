"""Voice capture and delivery through real Core contracts with only transport mocked."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

import companion_delivery_protocol as wire
from companion import delivery_reconciliation, owner_authority
from companion.model import ModelClient
from companion.voice_config import VoiceReplyPolicy
from companion.voice_jobs import VoiceJobs
from companion.voice_protocol import VoiceLeaseRef
from companion_onebot_bridge import (
    owner_handoff,
    owner_native_resolution,
    pending_media,
    voice_runtime,
)
from companion_onebot_bridge.capture import prepare_owner_input
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.onebot_client import OneBotClient
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.onebot_events import raw_event
from tests.test_onebot_voice_media import URL, public, segment, wav
from tests.test_prepare_owner_input import native_spool
from tests.test_prepare_owner_input import window as owner_window
from tests.test_voice_jobs import claim, control, result, voice_config


async def capture(window, client, protocol, *, record=True, number=1, flush=True):
    body = {"message": [segment()]} if record else {"text": "请继续讲讲"}
    frame = json.dumps(raw_event(number, time=int(window.clock().timestamp()), **body))
    prepared = await prepare_owner_input(window, client, OneBotClient(window.config, protocol),
                                         frame, window.clock(), ("127.0.0.1", 9000))
    event = prepared["event"]
    window.persist_capture(event, frame, received_at=window.clock(), peer=("127.0.0.1", 9000),
                            media=prepared["media"])
    if flush:
        assert await window.flush_one(client)
    return event


@asynccontextmanager
async def fixture(core, tmp_path, *, native=None, voice=True, asr=True, ws_url=None):
    def reply(request):
        if native:
            response = native(request)
            if response is not None:
                return response
        if request.url.path == "/get_record":
            assert json.loads(request.content) == {"file": "cache-123.amr"}
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {
                "file": URL, "url": URL, "file_size": str(len(wav())),
                "file_name": "cache-123.amr",
            }})
        values = {"/get_login_info": {"user_id": int(core.settings.bot_qq_id)},
                  "/get_status": {"online": True, "good": True},
                  "/get_version_info": {"app_name": "SnowLuma", "app_version": "2.0.3-node"}}
        assert request.url.path in values, request.url.path
        return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                       "data": values[request.url.path]})

    core.settings.voice = voice_config(reply=VoiceReplyPolicy(mode="auto"), asr={"enabled": asr})
    async with owner_window(core, tmp_path, voice=voice) as (_, config, plan):
        if ws_url:
            config = replace(config, onebot_ws_url=ws_url)
        spool, runtime = native_spool(core, tmp_path, config, plan)
        try:
            async with private_http_client(config.onebot_http_url, config.onebot_token,
                                           transport=httpx.MockTransport(reply)) as protocol, \
                    private_http_client(config.core_url, config.bridge_token) as client:
                yield runtime, client, protocol, [], plan
        finally:
            spool.close()


def count(runtime, table, where="1"):
    return runtime.db.execute("SELECT COUNT(*) FROM " + table + " WHERE " + where).fetchone()[0]


def media_row(runtime):
    return pending_media._execute(runtime, "SELECT * FROM automatic_media").fetchone()


def pump(window, client, protocol, *, download=None):
    return pending_media.MediaPump(window, client, protocol=OneBotClient(window.config, protocol),
        voice_resolver=public,
        voice_transport=httpx.MockTransport(download or (lambda request: httpx.Response(
            200, content=wav(), headers={"Content-Type": "audio/wav"}))))


async def tick(window, client, protocol):
    operation = OwnerSendGuard(window, client)
    return await deliver_tick(client, OwnerOneBot(operation, protocol),
                              window.spool, guard=operation)


def finish_tts(core, *, seconds=3):
    jobs = VoiceJobs(core.store, core.settings, core.clock)
    _, response = claim(jobs)
    ref = VoiceLeaseRef.from_lease(response.lease)
    jobs.input("local", ref)
    value = result(ref, seconds=seconds)
    control(jobs, ref, "RESULT_READY", "READY", result_sha256=value.result_sha256)
    jobs.submit("local", value)
    return value


def finish_asr(core, text):
    jobs = VoiceJobs(core.store, core.settings, core.clock)
    _, response = claim(jobs)
    ref = VoiceLeaseRef.from_lease(response.lease)
    jobs.input("local", ref)
    row = core.store.one("SELECT * FROM voice_jobs WHERE job_id=?", (ref.job_id,))
    value = result(ref, operation="asr", seconds=row["input_duration_seconds"],
                   partial=bool(row["source_partial"]), text=text)
    control(jobs, ref, "RESULT_READY", "READY", result_sha256=value.result_sha256)
    jobs.submit("local", value)


async def ready_reply(core, window, client, protocol):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "choices": [{"message": {"content": "这是一条合成语音回复。"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }))) as model:
        core.model = ModelClient(core.settings, model)
        await capture(window, client, protocol, record=False)
        assert await core.run_once("voice-boundary-synthetic")
    assert not await tick(window, client, protocol)
    return finish_tts(core)


def test_voice_scope_closed_keeps_record_placeholder_without_native_fetch(core, tmp_path):
    calls = []

    def native(request):
        calls.append(request.url.path)

    async def scenario():
        async with fixture(core, tmp_path, native=native, voice=False) as (
            window, client, protocol, _, _,
        ):
            await capture(window, client, protocol)
            assert not pending_media.exists(window)
            assert "/get_record" not in calls
            assert not core.store.one("SELECT 1 FROM voice_jobs")

    run(scenario())


def test_tts_only_scope_does_not_fetch_or_decode_an_owner_record(core, tmp_path):
    calls = []

    async def scenario():
        async with fixture(core, tmp_path, asr=False,
                           native=lambda request: calls.append(request.url.path)) as values:
            window, client, protocol, _, _ = values
            await capture(window, client, protocol)
            await pump(window, client, protocol).execute(media_row(window))
            assert media_row(window)["state"] == "DONE"
            assert "/get_record" not in calls
            assert not core.store.rows("SELECT * FROM voice_jobs")
            assert not (window.spool.directory / "voice-work").exists()
            window.verify()

    run(scenario())


def test_real_http_capture_asr_independent_chat_worker_and_tts_native_delivery(core, tmp_path):
    import pickle
    import sys
    from pathlib import Path

    from websockets.asyncio.server import serve

    from companion_onebot_bridge.app import receive, transfer_media, upload
    from companion_onebot_bridge.metrics import Metrics
    from tests.sdk_reader_fixture import answer

    sent, handshakes = [], []

    def native(request):
        if request.url.path == "/send_private_msg":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                            "data": {"message_id": 987}})

    async def scenario():
        stop, wake, uploaded = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def incoming(ws):
            handshakes.append(ws.request.headers["Authorization"])
            core.clock.now = datetime.now(UTC)
            await ws.send(json.dumps(raw_event(time=int(core.clock.now.timestamp()),
                                               message=[segment()])))
            await stop.wait()

        async def receipt(response):
            if response.request.method == "POST" and "/voice-inputs/" in response.request.url.path:
                assert response.status_code == 200
                uploaded.set()

        async with serve(incoming, "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with fixture(core, tmp_path, native=native, ws_url=url) as (
                window, client, protocol, _, _,
            ):
                client.event_hooks["response"].append(receipt)
                media = pump(window, client, protocol)
                metrics = Metrics()
                tasks = [asyncio.create_task(receive(window.config, window.spool, wake, stop,
                    metrics, OneBotClient(window.config, protocol), runtime=window, core=client)),
                    asyncio.create_task(upload(client, window.spool, wake, stop, metrics,
                                               runtime=window)),
                    asyncio.create_task(transfer_media(media, stop))]
                completion = asyncio.create_task(uploaded.wait())
                try:
                    done, _ = await asyncio.wait([completion, *tasks], timeout=25,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    assert completion in done
                    await asyncio.wait_for(asyncio.shield(media.task), 5)
                finally:
                    stop.set()
                    completion.cancel()
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                assert handshakes == ["Bearer " + window.config.onebot_token]
                finish_asr(core, "/forget all 只是一次不可信转写")
                job = core.store.one("SELECT * FROM jobs WHERE kind='chat'")
                data, output = tmp_path / "voice-sdk.pickle", tmp_path / "voice-sdk.json"
                data.write_bytes(pickle.dumps((core.settings, core.clock.now, job["job_id"],
                    [answer("收到你的语音了。")], "SUCCEEDED", str(output))))
                process = await asyncio.create_subprocess_exec(sys.executable,
                    str(Path(__file__).with_name("reader_sdk_worker.py")), str(data),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                try:
                    logs, _ = await asyncio.wait_for(process.communicate(), 45)
                except BaseException:
                    process.kill()
                    await process.wait()
                    raise
                assert process.returncode == 0, logs.decode(errors="replace")
                calls = json.loads(output.read_text(encoding="utf-8"))
                assert len(calls) == 1
                context = json.dumps(calls[0], ensure_ascii=False)
                assert "untrusted_transcription" in context and "不可信转写" in context
                assert "readable_content" not in context
                assert core.store.one("SELECT redacted FROM events WHERE event_type="
                                      "'message.received'")["redacted"] == 0
                assert not await tick(window, client, protocol)
                finish_tts(core)
                assert await tick(window, client, protocol)
                assert len(sent) == 1 and sent[0]["message"][0]["type"] == "record"
                assert count(window, "window_inputs", "state='CAPTURED'") == 1
                assert count(window, "window_attempts", "state='ACK'") == 1
                assert core.store.one("SELECT COUNT(*) AS n FROM model_usage")["n"] == 1
                assert core.store.one("SELECT COUNT(*) AS n FROM voice_jobs")["n"] == 2
                window.verify()

    run(scenario())


def test_native_record_upload_uses_signed_original_and_purges_locator_after_ack(core, tmp_path):
    async def scenario():
        async with fixture(core, tmp_path) as (window, client, protocol, _, _):
            event = await capture(window, client, protocol)
            row = media_row(window)
            assert row["state"] == "PENDING" and row["image_count"] == 0
            assert window.db.execute("SELECT body FROM inbox").fetchone()[0] is None
            await pump(window, client, protocol).execute(row)
            row = media_row(window)
            assert row["state"] == "DONE" and row["manifest_json"] is None
            assert row["next_slot"] == 1
            job = core.store.one("SELECT * FROM voice_jobs")
            assert job["operation"] == "asr" and job["state"] == "QUEUED"
            assert job["event_id"] == event["event_id"] and job["source_segment_index"] == 0
            assert job["source_input_sha256"] == window.db.execute(
                "SELECT input_hash FROM window_inputs").fetchone()[0]
            assert not list((window.spool.directory / "voice-work").iterdir())
            assert count(window, "window_inputs") == 1 and count(window, "window_attempts") == 0
            assert URL not in window.path.read_text("utf-8")
            window.verify()

    run(scenario())


def test_lost_voice_upload_ack_recovers_only_matching_core_job_without_refetch(core, tmp_path):
    gets = []

    async def scenario():
        async with fixture(core, tmp_path) as (window, client, protocol, _, _):
            await capture(window, client, protocol)
            media = pump(window, client, protocol, download=lambda request: (
                gets.append(request.url) or httpx.Response(200, content=wav(),
                                                          headers={"Content-Type": "audio/wav"})))

            async def lost(response):
                if response.request.method == "POST" and "/voice-inputs/" in (
                    response.request.url.path):
                    raise httpx.ReadError("synthetic lost upload response")

            client.event_hooks["response"].append(lost)
            with pytest.raises(httpx.ReadError):
                await media.execute(media_row(window))
            client.event_hooks["response"].remove(lost)
            row = media_row(window)
            assert row["state"] == "UPLOADING" and len(gets) == 1
            await media.execute(row)
            assert len(gets) == 1
            assert window.db.execute("SELECT state FROM automatic_media").fetchone()[0] == "DONE"
            assert core.store.one("SELECT COUNT(*) AS n FROM voice_jobs")["n"] == 1
            window.verify()

    run(scenario())


def test_crash_after_voice_download_before_upload_can_resume_without_duplicate_asr(
    core, tmp_path, monkeypatch,
):
    from companion_onebot_bridge import voice_media

    gets = []
    prepare = voice_media.prepare_asr

    async def crash(*args, **kwargs):
        raise RuntimeError("synthetic crash before upload")

    async def scenario():
        async with fixture(core, tmp_path) as (window, client, protocol, _, _):
            await capture(window, client, protocol)
            media = pump(window, client, protocol, download=lambda request: (
                gets.append(request.url) or httpx.Response(200, content=wav(),
                                                          headers={"Content-Type": "audio/wav"})))
            monkeypatch.setattr(voice_media, "prepare_asr", crash)
            with pytest.raises(RuntimeError, match="synthetic crash"):
                await media.execute(media_row(window))
            row = media_row(window)
            assert row["state"] == "DOWNLOADING" and len(gets) == 1
            assert not core.store.one("SELECT 1 FROM voice_jobs")
            monkeypatch.setattr(voice_media, "prepare_asr", prepare)
            await media.execute(row)
            assert len(gets) == 2
            assert window.db.execute("SELECT state FROM automatic_media").fetchone()[0] == "DONE"
            assert core.store.one("SELECT COUNT(*) AS n FROM voice_jobs")["n"] == 1
            window.verify()
    run(scenario())


def test_record_alias_drift_is_unavailable_and_never_fetches_different_audio(core, tmp_path):
    def native(request):
        if request.url.path == "/get_record":
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {
                "file": URL + "changed", "url": URL + "changed", "file_size": "100",
                "file_name": "cache-123.amr",
            }})

    async def scenario():
        async with fixture(core, tmp_path, native=native) as (window, client, protocol, _, _):
            await capture(window, client, protocol)
            media = pump(window, client, protocol, download=lambda _: pytest.fail(
                "changed original source must never reach public GET"))
            await media.execute(media_row(window))
            assert not core.store.one("SELECT 1 FROM voice_jobs")
            row = media_row(window)
            assert row["state"] == "DONE" and row["manifest_json"] is None
            assert json.loads(row["receipt_json"])["reason"] == "VOICE_RECORD_SOURCE_CHANGED"
            assert window.row()["state"] == "ACTIVE"

    run(scenario())


@pytest.mark.parametrize("failure", ["request", "receipt"])
def test_voice_native_integrity_failure_is_not_an_unavailable_attachment(core, tmp_path, failure):
    calls = []

    def native(request):
        calls.append(request.url.path)
        if failure == "receipt" and request.url.path == "/get_record":
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {
                "file": URL, "url": URL, "file_size": str(len(wav())),
                "file_name": "cache-123.amr", "unexpected": "changed protocol receipt"}})

    async def mutate(request):
        if failure == "request" and request.url.path == "/get_record":
            request.headers["Authorization"] = "Bearer changed-after-preparation"

    async def scenario():
        async with fixture(core, tmp_path, native=native) as (window, client, protocol, _, _):
            await capture(window, client, protocol)
            protocol.event_hooks["request"].append(mutate)
            media = pump(window, client, protocol, download=lambda _: pytest.fail(
                "native integrity failure must never reach public GET"))
            code = "VOICE_ACTION_CHANGED" if failure == "request" else "VOICE_FIELDS_INVALID"
            with pytest.raises(ValueError, match=code):
                await media.execute(media_row(window))
            row = media_row(window)
            assert row["state"] == "DOWNLOADING" and row["manifest_json"]
            assert not core.store.rows("SELECT * FROM voice_jobs")
            assert calls == ([] if failure == "request" else ["/get_record"])
            window.verify()

    run(scenario())


@pytest.mark.parametrize("failure", [None, "lost_ack"])
def test_large_tts_lease_sends_only_record_with_actual_message_ack(core, tmp_path, failure):
    sent, checks = [], []

    def native(request):
        if request.url.path == "/send_private_msg":
            sent.append(json.loads(request.content))
            if failure:
                raise httpx.ReadTimeout("synthetic lost record response")
            return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                            "data": {"message_id": 987}})

    async def scenario():
        async with (
            fixture(core, tmp_path, native=native) as (window, client, protocol, _, _),
            httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
                "choices": [{"message": {"content": "这是一条合成语音回复。"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }))) as model
        ):
            core.model = ModelClient(core.settings, model)
            await capture(window, client, protocol, record=False)
            assert await core.run_once("voice-reply-synthetic")
            assert not await tick(window, client, protocol)
            audio = finish_tts(core)

            async def inspect(request):
                if request.url.path.endswith("/send-check"):
                    checks.append(json.loads(request.content))

            client.event_hooks["request"].append(inspect)
            assert await tick(window, client, protocol)
            assert len(sent) == len(checks) == 1
            assert len(json.dumps(sent[0])) > 65536
            assert sent[0]["message"] == [{"type": "record", "data": {
                "file": "base64://" + audio.payload.audio_base64}}]
            assert checks[0]["voice"]["audio_sha256"] == audio.payload.audio_sha256
            outbox = core.store.one("SELECT * FROM outbox")
            expected = "DELIVERY_UNCERTAIN" if failure else "SENT"
            assert outbox["state"] == expected and outbox["attempt_count"] == 1
            assert outbox["provider_message_id"] == (None if failure else "987")
            assert outbox["native_ack_json"] is None
            local = json.loads(window.db.execute("SELECT body FROM results").fetchone()[0])
            assert local["result"] == expected
            assert count(window, "window_attempts") == 1

    run(scenario())


def test_voice_public_get_and_decode_cannot_continue_after_pending_owner_input(core, tmp_path):
    async def scenario():
        async with fixture(core, tmp_path) as (window, client, protocol, _, _):
            event = await capture(window, client, protocol)
            row = media_row(window)
            original = pending_media.manifest(window, row)[0]
            bound = voice_runtime.source(window, event["event_id"], original)
            await capture(window, client, protocol, record=False, number=2, flush=False)
            with pytest.raises(pending_media.SourceCancelled):
                await voice_runtime.status(window, client, bound, original,
                                           lambda: asyncio.sleep(0))
            assert window.db.execute("SELECT state FROM automatic_media").fetchone()[0] == (
                "CANCELLED")
            assert not core.store.one("SELECT 1 FROM voice_jobs")
            assert window.row()["state"] == "ACTIVE"

    run(scenario())


@pytest.mark.parametrize("phase", ["body", "path", "binding", "stream", "stop", "owner"])
def test_voice_final_native_boundary_binds_audio_and_source_after_core_await(core, tmp_path, phase):
    sent, pending = [], []

    def native(request):
        if request.url.path == "/send_private_msg":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                            "data": {"message_id": 987}})

    class ChangedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("replacement stream must be sealed before native I/O")
            yield b"unreachable"

    async def scenario():
        async with fixture(core, tmp_path, native=native) as (window, client, protocol, _, _):
            audio = await ready_reply(core, window, client, protocol)
            operation = OwnerSendGuard(window, client)

            async def retain(request):
                if request.url.path == "/send_private_msg":
                    pending.append(request)

            async def changed(response):
                if not response.request.url.path.endswith("/send-check"):
                    return
                assert response.status_code == 200 and len(pending) == 1
                request = pending[0]
                if phase == "body":
                    body = json.loads(request.content)
                    body["message"][0]["data"]["file"] = "base64://AA=="
                    request._content = json.dumps(body).encode()
                elif phase == "path":
                    request.url = request.url.copy_with(path="/send_group_msg")
                elif phase == "binding":
                    operation.current_item["voice"]["binding"]["audio_sha256"] = "0" * 64
                elif phase == "stream":
                    request.stream = ChangedStream()
                elif phase == "stop":
                    window.stop()
                else:
                    await capture(window, client, protocol, record=False, number=2, flush=False)

            protocol.event_hooks["request"].append(retain)
            client.event_hooks["response"].append(changed)
            if phase == "binding":
                with pytest.raises(ValueError, match="OWNER_DELIVERY_BINDING_CHANGED"):
                    await deliver_tick(client, OwnerOneBot(operation, protocol),
                                       window.spool, guard=operation)
                window.stop()
                assert not await window.spool.flush_result(client)
                assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"
            else:
                assert await deliver_tick(client, OwnerOneBot(operation, protocol),
                                          window.spool, guard=operation)
            if phase == "stream":
                assert len(sent) == 1 and sent[0]["message"] == [{"type": "record", "data": {
                    "file": "base64://" + audio.payload.audio_base64}}]
                assert core.store.one("SELECT state FROM outbox")["state"] == "SENT"
            else:
                assert sent == []
                expected = ("NOT_SENT" if phase == "owner" else
                            "RESERVED" if phase == "binding" else "UNKNOWN")
                assert count(window, "window_attempts", f"state='{expected}'") == 1
                body = json.loads(window.db.execute("SELECT body FROM results").fetchone()[0])
                assert body["result"] == ("NOT_SENT" if phase == "owner" else "DELIVERY_UNCERTAIN")
                assert body.get("provider_message_id") is None

    run(scenario())


@pytest.mark.parametrize("outcome", ["SENT", "CANCELLED"])
def test_unknown_voice_owner_resolution_retains_voice_check_and_allows_handoff(
    core, tmp_path, outcome,
):
    from companion.owner_authority_protocol import AuthorityStop
    from tests.test_owner_authority import activate

    sent = []

    def native(request):
        if request.url.path == "/send_private_msg":
            sent.append(request)
            raise httpx.ReadTimeout("synthetic voice acknowledgement lost")

    async def scenario():
        async with fixture(core, tmp_path, native=native) as (window, client, protocol, _, _):
            audio = await ready_reply(core, window, client, protocol)
            policy = owner_authority.current(core.store, core.settings, core.clock).policy
            assert await tick(window, client, protocol)
            window.stop()
            owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                operation_id="voice-bridge-stop", authority=window.authority))
            attempt = dict(pending_media._execute(window,
                "SELECT * FROM window_attempts").fetchone())
            original = dict(pending_media._execute(window, "SELECT * FROM results").fetchone())
            config = window.config
            window.spool.close()
            core.clock.advance(60)
            with Spool(config, reconciliation_only=True) as spool:
                old = OwnerRuntime(spool, clock=lambda: core.clock.now)
                await owner_native_resolution.prepare(old, client, attempt["message_id"])
                proof = json.loads(owner_native_resolution.rows(old)[0]["proof_json"])
                assert proof["voice_check"]["audio_sha256"] == audio.payload.audio_sha256
                assert proof["reply_revision"] == 1
                delivery_reconciliation.resolve(core.store, core.settings,
                    attempt["message_id"], outcome, core.clock(),
                    operation_id="voice-owner-decision",
                    operation_sha256=wire.digest([attempt["message_id"], outcome]))
                await owner_native_resolution.reconcile(old, client, attempt["message_id"])
                assert dict(pending_media._execute(old,
                    "SELECT * FROM window_attempts").fetchone()) == attempt
                assert dict(pending_media._execute(old,
                    "SELECT * FROM results").fetchone()) == original
                assert old.db.execute("SELECT state FROM window_inputs").fetchone()[0] == "CAPTURED"
                await owner_handoff.prepare(old, client)
                approved, _ = activate(core, policy, revision=2, operation="voice-owner-successor")
                successor = approved["authority"]
                assert (await owner_handoff.handoff(old, client, successor))["state"] == "ACTIVE"
            with Spool(replace(config, owner_authority=successor)) as spool:
                restarted = OwnerRuntime(spool, clock=lambda: core.clock.now)
                restarted.verify()
                assert not await restarted.flush_one(client)
                assert len(sent) == 1

    run(scenario())
