import asyncio
import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError

from companion.android_observer import AndroidObserver
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.media import (
    MediaService,
    android_text,
    erase_media_links,
)
from companion.media_api import register_media_routes
from companion.media_config import MediaConfig, MediaEndpoint
from companion.media_provider import HTTPMediaProvider, MediaResult
from companion.store import Store
from tests.conftest import ingest, run
from tests.postgres_fixture import logical_state
from tests.support.media import AUDIO, IMAGE, XML, FakeMedia, chunk, process, service, start


def test_media_defaults_disabled_and_explicit_device_grants(core):
    disabled = MediaService(core.store, core.settings, core.clock, core.observations)
    with pytest.raises(CoreError, match="MEDIA_DISABLED"):
        start(disabled)
    media = service(core)
    core.settings.voice_enabled = False
    with pytest.raises(CoreError, match="CAPABILITY_DISABLED"):
        start(media)
    assert core.store.rows("SELECT * FROM media_sessions") == []


def test_session_token_scope_expiry_and_stop(core):
    media = service(core)
    session = start(media, duration_seconds=5)
    with pytest.raises(CoreError, match="UNAUTHORIZED"):
        media.status(session["session_id"], "wrong-token")
    with pytest.raises(CoreError, match="MODALITY_NOT_GRANTED"):
        process(media, session, chunk("camera"))
    stored = core.store.one("SELECT * FROM media_sessions")
    assert stored["token_hash"] == hashlib.sha256(session["session_token"].encode()).hexdigest()
    assert session["session_token"] not in str(stored)
    core.clock.advance(6)
    with pytest.raises(CoreError, match="CLOSED"):
        process(media, session, chunk())
    assert media.close(session["session_id"], session["session_token"])["state"] == "CLOSED"
    assert media.close(session["session_id"], owner=True)["state"] == "CLOSED"
    assert not media.provider.calls


@pytest.mark.parametrize(
    "operation,source,kind",
    [
        ("audio", "voice", "audio_transcript"),
        ("camera", "camera", "image_analysis"),
        ("android", "android", "text"),
    ],
)
def test_media_creates_actual_observation_without_raw_retention_or_delivery(
    core, operation, source, kind
):
    media = service(core)
    session = start(media, [source])
    result = process(media, session, chunk(operation))
    row = core.store.one('SELECT * FROM observations WHERE event_id=%s', (result["event_id"],))
    assert row["source_kind"] == kind and row["result_status"] == "FETCHED"
    assert row["device_id"] == ("adb:device-1" if source == "android" else "browser-owner")
    evidence = json.loads(row["evidence_json"])
    raw = {"audio": AUDIO, "camera": IMAGE, "android": XML}[operation]
    assert evidence["media_sha256"] == hashlib.sha256(raw).hexdigest()
    assert evidence["processor_id"]
    dump = json.dumps(logical_state(core.store), ensure_ascii=False)
    assert base64.b64encode(raw).decode() not in dump
    assert "hidden-password" not in dump
    assert core.store.rows("SELECT * FROM jobs") == core.store.rows("SELECT * FROM outbox") == []
    assert len(media.provider.calls) == (0 if operation == "android" else 1)


def test_tts_uses_budget_returns_audio_and_does_not_cache_or_resend(core):
    media = service(core)
    session = start(media)
    result = process(media, session, chunk("speech"))
    assert base64.b64decode(result["audio_base64"]).startswith(b"ID3")
    assert core.observations.recent() == []
    with pytest.raises(CoreError, match="ALREADY_SUCCEEDED"):
        process(media, session, chunk("speech"))
    assert len(media.provider.calls) == 1
    assert "valid-audio-fixture" not in json.dumps(logical_state(core.store), ensure_ascii=False)
    usage = core.store.one("SELECT * FROM model_usage")
    assert usage["billed_micro_usd"] == 100 and usage["state"] == "SETTLED"
    assert usage["is_estimate"] == 1


def test_provider_failure_keeps_reservation_and_blocks_duplicate_retry(core):
    media = service(core)
    session = start(media)
    media.provider.fail = True
    with pytest.raises(CoreError, match="UNCERTAIN"):
        process(media, session, chunk())
    with pytest.raises(CoreError, match="ALREADY_UNKNOWN"):
        process(media, session, chunk())
    with pytest.raises(CoreError, match="IDEMPOTENCY_CONFLICT"):
        process(media, session, chunk(data=AUDIO + b"changed"))
    assert len(media.provider.calls) == 1
    assert core.store.one("SELECT * FROM model_usage")["billed_micro_usd"] == 100
    assert core.store.one("SELECT * FROM model_usage")["state"] == "UNKNOWN"


@pytest.mark.parametrize(
    "change,error",
    [
        ("revoke", "CONSENT_REQUIRED"),
        ("privacy", "PRIVACY_CHANGED"),
        ("disabled", "MEDIA_DISABLED"),
        ("expiry", "SESSION_CLOSED"),
    ],
)
def test_consent_privacy_and_feature_rechecked_after_provider(core, change, error):
    media = service(core)
    session = start(media, duration_seconds=5)

    async def callback():
        if change == "revoke":
            core.store.execute('UPDATE consents SET revoked_at=%s', (core.clock(),))
        elif change == "privacy":
            core.observations.consents.revoke(session["consent_id"])
        elif change == "disabled":
            media.options.enabled = False
        else:
            core.clock.advance(6)

    media.provider.callback = callback
    with pytest.raises(CoreError, match=error):
        process(media, session, chunk())
    assert core.observations.recent() == []
    assert core.store.one("SELECT * FROM model_usage")["state"] == "UNKNOWN"


def test_owner_stop_cancels_inflight_provider_and_retains_unknown_cost(core):
    media = service(core)
    session = start(media)

    async def scenario():
        entered = asyncio.Event()

        async def callback():
            entered.set()
            await asyncio.Event().wait()

        media.provider.callback = callback
        task = asyncio.create_task(
            media.process(session["session_id"], session["session_token"], chunk())
        )
        await entered.wait()
        media.close(session["session_id"], owner=True)
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert core.observations.recent() == []
    assert core.store.one("SELECT * FROM media_requests")["state"] == "UNKNOWN"
    assert media.inflight == {}


@pytest.mark.parametrize("limit", ["session", "daily"])
def test_reservation_limits_checked_before_any_provider_call(core, limit):
    media = service(core)
    session = start(media, max_bytes=1024)
    if limit == "session":
        item = chunk(data=AUDIO + b"x" * 1100)
    else:
        item = chunk()
        if limit == "daily":
            media.options.max_daily_bytes = 0
        elif limit == "cost":
            media.options.daily_micro_usd = 0
        else:
            core.settings.monthly_limit_micro_usd = 0
    with pytest.raises(CoreError, match="BUDGET_EXHAUSTED"):
        process(media, session, item)
    assert not media.provider.calls
    assert not core.store.rows("SELECT * FROM media_requests")


def test_concurrent_requests_cannot_overspend_remaining_session_budget(core):
    media = service(core)
    session = start(media, max_bytes=1024)
    data = AUDIO + b"x" * 600

    async def scenario():
        return await asyncio.gather(
            *(
                media.process(
                    session["session_id"],
                    session["session_token"],
                    chunk(key="parallel-" + str(i), data=data),
                )
                for i in range(3)
            ),
            return_exceptions=True,
        )

    results = run(scenario())
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, CoreError) for result in results) == 2
    assert len(media.provider.calls) == 1


@pytest.mark.parametrize("tool_state", ["RUNNING", "UNKNOWN"])
def test_media_preserves_tool_liability_without_global_currency_refusal(core, tool_state):
    from tests.support.executive import FakeExecutor, configured, proposal

    executor = FakeExecutor()
    executive = configured(core, executor)
    core.settings.monthly_limit_micro_usd = 1000
    executive.config.endpoints["windows"].max_cost_micro_usd = 1000
    executive.submit(proposal())
    if tool_state == "RUNNING":
        executive.begin(executive.claim())
    else:
        executor.fail = True
        assert run(executive.run_one())
    assert core.store.one("SELECT state,billed_micro_usd FROM tool_tasks") == {
        "state": tool_state,
        "billed_micro_usd": 1000,
    }
    media = service(core)
    process(media, start(media), chunk())
    assert len(media.provider.calls) == 1
    assert core.store.rows("SELECT * FROM media_requests")
    assert core.store.rows("SELECT * FROM model_usage WHERE scenario='media'")
    assert core.store.one("SELECT state,billed_micro_usd FROM tool_tasks") == {
        "state": tool_state, "billed_micro_usd": 1000}


def test_credentials_rejected_before_speech_and_in_provider_output(core):
    media = service(core)
    session = start(media)
    item = chunk("speech").model_copy(update={"text": media.options.tts.key.get_secret_value()})
    with pytest.raises(CoreError, match="CREDENTIALS"):
        process(media, session, item)
    assert media.provider.calls == []

    async def leak(*args):
        return MediaResult(text=session["session_token"])

    media.provider.process = leak
    with pytest.raises(CoreError, match="CREDENTIALS"):
        process(media, session, chunk())
    assert core.observations.recent() == []


def test_android_parser_excludes_passwords_and_rejects_entity_expansion():
    assert android_text(XML) == "Visible title"
    with pytest.raises(CoreError, match="INVALID_ANDROID"):
        android_text(
            b'<!DOCTYPE doc [<!ENTITY x "secret">]><hierarchy><node text="&x;" /></hierarchy>'
        )


def test_erase_media_provenance_hash(core):
    media = service(core)
    result = process(media, start(media), chunk())
    with core.store.transaction():
        erase_media_links(core.store, [result["event_id"]])
    row = core.store.one("SELECT * FROM media_requests")
    assert row["fingerprint"] == "" and row["event_id"] is None


def test_media_commit_is_atomic_with_actual_erasure_on_another_connection(core):
    media = service(core)
    session = start(media)
    append = media.observations.append
    attempted, erased = Event(), Event()
    futures, committed = [], []

    def erase_after_lock(event_id):
        other = Store(core.store.database, core.settings.state_directory)
        try:
            attempted.set()
            with other.transaction(control_write=True):
                committed.append(other.one("SELECT state,event_id FROM media_requests"))
                core.ledger.expire_events(other, [event_id], core.clock())
                erase_media_links(other, [event_id])
        finally:
            other.close()
            erased.set()

    with ThreadPoolExecutor(max_workers=1) as pool:

        def append_and_race(adapter_id, observation):
            result = append(adapter_id, observation)
            futures.append(pool.submit(erase_after_lock, observation.event_id))
            assert attempted.wait(2)
            # The independent writer waits for evidence AND its request link to commit.
            assert not erased.wait(0.05)
            return result

        media.observations.append = append_and_race
        result = process(media, session, chunk())
        futures[0].result(timeout=5)
    assert committed == [{"state": "SUCCEEDED", "event_id": result["event_id"]}]
    assert core.store.one(
        'SELECT redacted FROM events WHERE event_id=%s', (result["event_id"],)
    ) == {"redacted": 1}
    request = core.store.one("SELECT * FROM media_requests")
    assert request["fingerprint"] == "" and request["event_id"] is None


def test_actual_erasure_before_final_media_transaction_suppresses_result(core):
    media = service(core)
    session = start(media)
    ingest(core, 90, "待遗忘的主人消息")

    async def erase_while_provider_runs():
        other = Store(core.store.database, core.settings.state_directory)
        try:
            with other.transaction(control_write=True):
                core.ledger.expire_events(other, ["evt_test_90"], core.clock())
        finally:
            other.close()

    media.provider.callback = erase_while_provider_runs
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        process(media, session, chunk())
    assert core.observations.recent() == []
    assert core.store.one("SELECT state FROM media_requests")["state"] == "UNKNOWN"


def test_request_settlement_failure_rolls_back_media_observation(core, monkeypatch):
    media = service(core)
    session = start(media)
    execute = core.store.execute

    def fail_settlement(sql, params=()):
        if "UPDATE media_requests SET state = 'SUCCEEDED'" in sql:
            raise RuntimeError("simulated commit failure")
        return execute(sql, params)

    monkeypatch.setattr(core.store, "execute", fail_settlement)
    with pytest.raises(RuntimeError, match="commit failure"):
        process(media, session, chunk())
    assert core.observations.recent() == []
    assert core.store.one("SELECT state,event_id FROM media_requests") == {
        "state": "UNKNOWN",
        "event_id": None,
    }


def test_restore_cannot_reauthorize_old_media_session(empty_database, core, tmp_path):
    media = service(core)
    session = start(media)
    key, archive, restored_state = (
        Fernet.generate_key(),
        tmp_path / "media.enc",
        tmp_path / "restored-state",
    )
    backup(core.settings.database, archive, key)
    restore(archive, empty_database, key, core.settings.erasure_ledger,
            state_directory=restored_state)
    restored = Core(
        core.settings.model_copy(update={
            "database": empty_database, "state_directory": str(restored_state),
        }), clock=core.clock
    )
    try:
        recovered = MediaService(
            restored.store,
            restored.settings,
            restored.clock,
            restored.observations,
            media.options,
            FakeMedia(),
        )
        with pytest.raises(CoreError, match="PRIVACY_CHANGED|CONSENT_REQUIRED|SESSION_CLOSED"):
            recovered.status(session["session_id"], session["session_token"])
        assert not recovered.provider.calls
    finally:
        restored.close()


def test_provider_oversize_response_and_fake_media_signature_fail_closed(core):
    media = service(core)
    session = start(media)
    with pytest.raises(CoreError, match="INVALID_MEDIA_BODY"):
        process(media, session, chunk(data=b"not an audio container"))
    assert not media.provider.calls
    provider = HTTPMediaProvider(
        MediaConfig(max_output_bytes=1024),
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=b"ID3" + b"x" * 2048, headers={"content-type": "audio/mpeg"}
            )
        ),
    )
    with pytest.raises(CoreError, match="RESPONSE_TOO_LARGE"):
        run(provider.process("speech", b"hello", "text/plain", media.options.tts))


@pytest.mark.parametrize("protocol", ["openai", "minimax"])
def test_tts_real_http_shape_and_bounded_memory_response(protocol):
    options = MediaConfig(speech_voice="fixture-voice")
    endpoint = MediaEndpoint(
        url="https://speech.example.test/speech",
        model="fixture-model",
        key="provider-secret",
        protocol=protocol,
    )

    def handler(request):
        assert request.headers["authorization"] == "Bearer provider-secret"
        body = json.loads(request.content)
        assert "provider-secret" not in request.content.decode()
        if protocol == "minimax":
            assert body["text"] == "你好" and body["voice_setting"]["voice_id"] == "fixture-voice"
            return httpx.Response(
                200, json={"base_resp": {"status_code": 0}, "data": {"audio": b"ID3response".hex()}}
            )
        assert body["input"] == "你好" and body["voice"] == "fixture-voice"
        return httpx.Response(200, content=b"ID3response", headers={"content-type": "audio/mpeg"})

    provider = HTTPMediaProvider(options, httpx.MockTransport(handler))
    result = run(provider.process("speech", "你好".encode(), "text/plain", endpoint))
    assert result.audio == b"ID3response"


@pytest.mark.parametrize("operation", ["audio", "camera"])
def test_stt_and_image_http_adapter(operation):
    endpoint = MediaEndpoint(url="https://media.example.test/process", model="fixture-model")

    def handler(request):
        if operation == "audio":
            assert request.headers["content-type"].startswith("multipart/form-data")
            assert AUDIO in request.content and b"fixture-model" in request.content
            return httpx.Response(200, json={"text": "transcribed voice"})
        body = json.loads(request.content)
        assert body["messages"][0]["content"][1]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": "visible image"}}]})

    provider = HTTPMediaProvider(MediaConfig(), httpx.MockTransport(handler))
    result = run(
        provider.process(
            operation,
            AUDIO if operation == "audio" else IMAGE,
            "audio/webm" if operation == "audio" else "image/jpeg",
            endpoint,
        )
    )
    assert result.text == ("transcribed voice" if operation == "audio" else "visible image")


def test_media_provider_redirect_not_followed_and_bad_url_rejected():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://untrusted.example/steal"})

    provider = HTTPMediaProvider(MediaConfig(), httpx.MockTransport(handler))
    with pytest.raises(CoreError, match="REJECTED"):
        run(
            provider.process(
                "audio", AUDIO, "audio/webm", MediaEndpoint(url="https://media.example.test/stt")
            )
        )
    assert len(calls) == 1
    with pytest.raises(ValidationError):
        MediaEndpoint(url="https://user:secret@example.test/stt")


def test_routes_require_owner_start_and_scoped_session_token(core):
    media = service(core)
    app = FastAPI()
    app.state.core = SimpleNamespace(media=media)

    async def owner(authorization: str = Header(default="")):
        if authorization != "Bearer owner-management":
            raise HTTPException(401)

    @app.exception_handler(CoreError)
    async def handle(request, exc):
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    register_media_routes(app, [Depends(owner)])
    with TestClient(app) as client:
        body = {"device_id": "browser-owner", "modalities": ["voice"]}
        assert client.post("/internal/v2/media/sessions", json=body).status_code == 401
        response = client.post(
            "/internal/v2/media/sessions",
            json=body,
            headers={"Authorization": "Bearer owner-management"},
        )
        assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
        session = response.json()
        path = "/internal/v2/media/sessions/" + session["session_id"]
        headers = {"Authorization": "Bearer " + session["session_token"]}
        assert client.get(path).status_code == 401
        assert client.get(path, headers=headers).status_code == 200
        assert client.post(path + "/revoke", headers=headers).status_code == 401
        assert (
            client.post(path + "/chunks", headers=headers, json=chunk().model_dump()).status_code
            == 200
        )
        assert client.post(path + "/close", headers=headers).status_code == 200
        assert client.get(path, headers=headers).status_code == 410
        page = client.get("/media")
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert client.get("/media/app.js").status_code == 200


def test_android_collector_validates_consent_before_and_after_readonly_capture():
    calls, commands = [], []

    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "device_id": "adb:device-1",
                    "modalities": ["android"],
                    "bytes_reserved": 0,
                    "max_bytes": 10000,
                    "max_chunk_bytes": 64000,
                },
            )
        assert base64.b64decode(json.loads(request.content)["data_base64"]) == XML
        return httpx.Response(200, json={"event_id": "obs_android"})

    def capture(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout=XML, returncode=0)

    observer = AndroidObserver(
        "https://core.example.test",
        "media_" + "a" * 32,
        "scoped-session",
        "device-1",
        transport=httpx.MockTransport(handler),
    )
    assert observer.capture(capture)["event_id"] == "obs_android"
    assert calls == ["GET", "GET", "POST"]
    assert commands == [["adb", "-s", "device-1", "exec-out", "uiautomator", "dump", "/dev/tty"]]


def test_android_denied_session_does_not_execute_adb():
    def denied(request):
        return httpx.Response(403)

    observer = AndroidObserver(
        "https://core.example.test",
        "media_" + "a" * 32,
        "scoped-session",
        "device-1",
        transport=httpx.MockTransport(denied),
    )
    with pytest.raises(httpx.HTTPStatusError):
        observer.capture(lambda *args, **kwargs: pytest.fail("ADB must not be invoked"))


def test_android_session_requires_canonical_adb_device_id(core):
    media = service(core)
    with pytest.raises(CoreError, match="ANDROID_DEVICE_ID_REQUIRED"):
        start(media, ["android"], device_id="friendly-device-label")
    assert core.store.rows("SELECT * FROM media_sessions") == []


@pytest.mark.parametrize("mismatch", ["before", "after"])
def test_android_serial_is_bound_before_capture_and_before_upload(mismatch):
    reads, commands = [], []

    def handler(request):
        assert request.method == "GET", "Mismatched capture must never be uploaded"
        reads.append(request)
        device = "adb:another-device" if mismatch == "before" or len(reads) > 1 else "adb:device-1"
        return httpx.Response(
            200,
            json={
                "device_id": device,
                "modalities": ["android"],
                "max_bytes": 10000,
                "bytes_reserved": 0,
                "max_chunk_bytes": 64000,
            },
        )

    def capture(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout=XML, returncode=0)

    observer = AndroidObserver(
        "https://core.example.test",
        "media_" + "a" * 32,
        "session-token",
        "device-1",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ValueError, match="ANDROID_DEVICE_MISMATCH"):
        observer.capture(capture)
    assert len(commands) == (0 if mismatch == "before" else 1)


def test_core_media_routes_and_secret_redaction(core):
    from pydantic import SecretStr

    from companion.api import create_app
    from companion.privacy import redact

    configured = service(core)
    core.settings.media = configured.options
    core.settings.control_token = SecretStr("owner-media-control-" * 3)
    owner = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
    bridge = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
    with TestClient(create_app(core.settings)) as client:
        body = {"device_id": "browser-owner", "modalities": ["voice"]}
        path = "/internal/v2/media/sessions"
        assert client.post(path, json=body, headers=bridge).status_code == 401
        result = client.post(path, json=body, headers=owner)
        assert result.status_code == 201
        assert client.app.state.core.status()["features"]["voice"] is True
        assert client.get("/media").status_code == 200
        session = result.json()
        assert (
            client.post(path + "/" + session["session_id"] + "/revoke", headers=owner).status_code
            == 200
        )
    assert redact("private-media-provider-key", core.settings) == "[REDACTED]"


def test_settings_load_nested_media_configuration(core, monkeypatch, tmp_path):
    from companion.config import Settings

    monkeypatch.setenv("COMPANION_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("COMPANION_DATABASE", core.settings.database.get_secret_value())
    monkeypatch.setenv("COMPANION_OWNER_QQ_ID", core.settings.owner_qq_id)
    monkeypatch.setenv("COMPANION_BOT_QQ_ID", core.settings.bot_qq_id)
    monkeypatch.setenv("COMPANION_BRIDGE_TOKEN", core.settings.bridge_token.get_secret_value())
    monkeypatch.setenv("COMPANION_MEDIA", '{"enabled":false,"max_session_seconds":60}')
    loaded = Settings.load()
    assert not loaded.media.enabled and loaded.media.max_session_seconds == 60


def test_zero_media_currency_amount_keeps_native_charge(core):
    media = service(core)
    media.options.daily_micro_usd = 0
    core.settings.monthly_limit_micro_usd = 0
    process(media, start(media), chunk())
    assert len(media.provider.calls) == 1
    usage = core.store.one("SELECT state,billed_micro_usd FROM model_usage WHERE scenario='media'")
    assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] > 0
