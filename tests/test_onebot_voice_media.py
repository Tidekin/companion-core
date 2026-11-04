"""Mock OneBot/CDN transport, independent Core DTO checks, and real bounded audio children."""

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import wave
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
import pysilk
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion.voice_jobs import UPLOAD_DOMAIN, VoiceASRProof
from companion.voice_protocol import (
    VoiceAudioResult,
    VoiceDeliveryBinding,
    VoiceDeliveryEnvelope,
    canonical_bytes,
)
from companion_onebot_bridge import voice_media as voice
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.onebot_client import OneBotClient, http_client
from tests.conftest import run
from tests.support.onebot_events import config, raw_event

NOW = datetime(2026, 10, 24, tzinfo=UTC)
URL = "https://multimedia.nt.qq.com.cn/download/voice?key=private-signed-token"
KEY = "11" * 32
AUTHORITY = {"version": 2, "grant_id": "grant_" + "a" * 32, "revision": 1,
             "policy_sha256": "a" * 64, "restore_generation": "voice-native-test"}
POLICY = "b" * 64


def wav(seconds=1, *, rate=16000, channels=1):
    result = BytesIO()
    with wave.open(result, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x01\x00" * (int(seconds * rate) * channels))
    return result.getvalue()


def segment(**changes):
    return {"type": "record", "data": {"file": "cache-123.amr",
                                        "url": URL, **changes}}


def original():
    return voice.original_records([segment()])[0]


def event():
    raw = raw_event(message=[segment()], time=int(NOW.timestamp()))
    return normalize(json.dumps(raw), config(), now=NOW.timestamp(),
                     rich_input_scope="references_v1")


async def valid():
    return None


async def public(host, port):
    assert (host, port) == ("multimedia.nt.qq.com.cn", 443)
    return ["93.184.216.34"]


@asynccontextmanager
async def protocol(handler=None, *, hooks=(), body=None):
    cfg = config(onebot_http_url="http://127.0.0.1:3000", onebot_ws_url="ws://127.0.0.1:3001")

    def default(request):
        assert request.url.path == "/get_record"
        assert json.loads(request.content) == {"file": "cache-123.amr"}
        assert request.headers["Authorization"] == "Bearer " + cfg.onebot_token
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": body or {
            "file": URL, "url": URL, "file_size": str(len(wav())), "file_name": "cache-123.amr",
        }})

    async with http_client(cfg.onebot_http_url, cfg.onebot_token,
                           transport=httpx.MockTransport(handler or default)) as client:
        client.event_hooks["request"] = list(hooks)
        yield OneBotClient(cfg, client)


async def fetch(*, action=None, get=None, check=valid, resolver=public, limit=8_000_000,
                record=None, **kwargs):
    async with protocol(action) as onebot:
        return await voice.fetch_record(
            record or original(), limit, onebot=onebot, check=check,
            synthetic=True, resolver=resolver,
            transport=httpx.MockTransport(get or (lambda request: httpx.Response(
                200, content=wav(), headers={"Content-Type": "audio/wav"}))), **kwargs,
        )


def downloaded(data=None):
    data = wav() if data is None else data
    return voice.DownloadedRecord(original(), data, "application/octet-stream",
                                  hashlib.sha256(data).hexdigest())


async def prepare(tmp_path, data=None, *, limits=voice.DEFAULT_LIMITS, check=valid, **kwargs):
    return await voice.prepare_asr(
        downloaded(data), event(), authority=AUTHORITY, capture_signing_key=KEY,
        check=check, work_dir=tmp_path / "owned-work", limits=limits, **kwargs,
    )


def assert_clean(tmp_path):
    work = tmp_path / "owned-work"
    assert not work.exists() or list(work.iterdir()) == []


def bound_source(**changes):
    owner = event()
    return voice.AuthenticatedASRSource(**{
        "event_id": owner["event_id"], "source_event_id": owner["source_event_id"],
        "source_input_sha256": voice.input_digest(owner["payload"]),
        "source_segment_index": original().index,
        "source_record_sha256": original().source_sha256, **changes,
    })


def test_capture_binding_without_plaintext_produces_same_signed_native_upload(tmp_path):
    source = bound_source()
    actual = run(voice.prepare_asr(
        downloaded(), source, authority=AUTHORITY, capture_signing_key=KEY,
        check=valid, work_dir=tmp_path / "owned-work",
    ))
    expected = run(prepare(tmp_path))
    assert actual.body == expected.body and actual.proof == expected.proof
    assert actual.proof_header == expected.proof_header
    assert_clean(tmp_path)
    with pytest.raises(FrozenInstanceError):
        source.source_input_sha256 = "0" * 64


@pytest.mark.parametrize("changes", [
    {"event_id": "evt_short"}, {"source_event_id": ""}, {"source_event_id": "bad\nsource"},
    {"source_input_sha256": "bad"}, {"source_record_sha256": "bad"},
    {"source_segment_index": True}, {"source_segment_index": 64},
])
def test_capture_source_binding_rejects_invalid_digests_identity_or_index(changes):
    with pytest.raises(ValueError):
        bound_source(**changes)


@pytest.mark.parametrize("changes", [
    {"source_segment_index": 1}, {"source_record_sha256": "0" * 64},
])
def test_capture_source_cannot_rebind_another_record_before_native_work(tmp_path, changes):
    with pytest.raises(ValueError, match="VOICE_RECORD_SOURCE_REQUIRED"):
        run(voice.prepare_asr(
            downloaded(), bound_source(**changes), authority=AUTHORITY, capture_signing_key=KEY,
            check=valid, work_dir=tmp_path / "owned-work",
        ))
    assert_clean(tmp_path)


def test_capture_source_binding_does_not_bypass_live_source_authority(tmp_path):
    async def recalled():
        raise ValueError("VOICE_SOURCE_RECALLED")

    with pytest.raises(ValueError, match="VOICE_SOURCE_RECALLED"):
        run(voice.prepare_asr(
            downloaded(), bound_source(), authority=AUTHORITY, capture_signing_key=KEY,
            check=recalled, work_dir=tmp_path / "owned-work",
        ))
    assert_clean(tmp_path)


def test_only_original_top_level_records_are_selected_and_message_url_is_never_authority():
    raw = segment()
    selected = voice.original_records([
        {"type": "text", "data": {"text": "hello"}}, raw,
        {"type": "node", "data": {"content": [raw]}}, raw,
    ])
    assert [(item.index, item.slot) for item in selected] == [(1, 0), (3, 1)]
    assert selected[0].source_sha256 == voice.digest(raw)
    assert "cache-123" not in repr(selected[0])
    assert voice.original_records("[CQ:record,file=https://untrusted]") == ()
    assert voice.original_records([{"type": "node", "data": {"content": [raw]}}]) == ()
    with pytest.raises(FrozenInstanceError):
        selected[0].file = "other"


@pytest.mark.parametrize("change", [{"user_id": 99999}, {"self_id": 99999},
                                    {"message_type": "group"}, {"post_type": "message_sent"}])
def test_non_owner_or_non_private_input_has_no_voice_capability(change):
    raw = raw_event(message=[segment()], **change)
    assert voice.owner_records(json.dumps(raw), config(), now=raw["time"]) == ()


@pytest.mark.parametrize("file", ["../private", "C:\\file", "file:///private", "base64://abc",
                                  "https://public.example/record", "", ".", ".."])
def test_record_cache_identity_cannot_be_a_caller_path_or_url(file):
    with pytest.raises(ValueError, match="VOICE_CACHE_ID_REQUIRED"):
        voice.original_records([segment(file=file)])


def test_get_record_and_download_reuse_private_and_public_destination_guards():
    seen = []

    def get(request):
        seen.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == "multimedia.nt.qq.com.cn"
        assert request.extensions["sni_hostname"] == "multimedia.nt.qq.com.cn"
        assert "Authorization" not in request.headers and "Cookie" not in request.headers
        return httpx.Response(200, content=wav(), headers={"Content-Type": "audio/wav"})

    result = run(fetch(get=get))
    assert result.body == wav() and result.original == original() and len(seen) == 1
    assert result.sha256 == hashlib.sha256(wav()).hexdigest()


@pytest.mark.parametrize("at", [1, 4, 7])
@pytest.mark.parametrize("failure", ["status", "timeout", "io"])
def test_core_guard_error_is_not_reclassified_as_a_voice_download_failure(at, failure):
    request = httpx.Request("GET", "http://127.0.0.1/core-check")
    error = {
        "status": httpx.HTTPStatusError("denied", request=request,
                                         response=httpx.Response(403, request=request)),
        "timeout": httpx.ReadTimeout("core check timeout", request=request),
        "io": OSError("local source custody failed"),
    }[failure]
    checks = 0

    async def check():
        nonlocal checks
        checks += 1
        if checks == at:
            raise error

    with pytest.raises(type(error)) as caught:
        run(fetch(check=check))
    assert caught.value is error
    assert checks == at


def test_voice_total_timeout_inside_core_guard_stays_a_core_check_error(monkeypatch):
    from companion_onebot_bridge.capture import diagnostic_fields

    monkeypatch.setattr(voice, "DOWNLOAD_TIMEOUT_SECONDS", 0.01)

    async def waiting_check():
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError) as caught:
        run(fetch(check=waiting_check))
    assert diagnostic_fields(caught.value) == {
        "capture_phase": "core_check", "capture_reason": "CAPTURE_TIMEOUT"}


@pytest.mark.parametrize("url", ["http://127.0.0.1/private", "file:///etc/passwd",
                                 "https://user:pass@multimedia.nt.qq.com.cn/record"])
def test_native_lookup_cannot_grant_local_paths_private_hosts_or_credentials(url):
    def action(request):
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {
            "file": url, "url": url, "file_size": "0", "file_name": "cache-123.amr",
        }})

    def forbidden(request):
        raise AssertionError("Forbidden media destination was reached")

    with pytest.raises(ValueError):
        run(fetch(action=action, get=forbidden,
                  record=voice.original_records([segment(url=url)])[0]))


def test_same_filename_cannot_select_a_different_or_refreshed_record_without_source_identity():
    def action(request):
        changed = URL + "-a-different-source"
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {
            "file": changed, "url": changed, "file_size": "0", "file_name": original().file,
        }})

    def forbidden(request):
        raise AssertionError("Source identity mismatch reached media download")

    with pytest.raises(ValueError, match="VOICE_RECORD_SOURCE_CHANGED"):
        run(fetch(action=action, get=forbidden))


def test_mixed_public_private_dns_rejected_before_get():
    async def mixed(*args):
        return ["93.184.216.34", "127.0.0.1"]

    with pytest.raises(ValueError, match="VOICE_NONPUBLIC_ADDRESS"):
        run(fetch(resolver=mixed))


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"Location": URL}),
    httpx.Response(200, content=b"x", headers={"Content-Range": "bytes 0-0/100"}),
    httpx.Response(200, content=b"x", headers={"Content-Length": "8000001"}),
    httpx.Response(200, content=b"x", headers={"Content-Type": "text/html"}),
])
def test_redirect_partial_oversize_or_html_response_is_rejected(response):
    with pytest.raises(ValueError):
        run(fetch(get=lambda request: response))


def test_record_size_mismatch_is_not_a_successful_partial_download():
    with pytest.raises(ValueError, match="VOICE_SOURCE_INCOMPLETE"):
        run(fetch(get=lambda request: httpx.Response(200, content=b"short")))


def test_final_native_request_guard_rejects_post_hook_body_or_token_changes():
    async def scenario():
        async def mutate(request):
            request.headers["Authorization"] = "Bearer different"

        def forbidden(request):
            raise AssertionError("Changed native request was dispatched")

        async with protocol(forbidden, hooks=[mutate]) as onebot:
            with pytest.raises(ValueError, match="VOICE_ACTION_CHANGED"):
                await voice.fetch_record(original(), onebot=onebot, check=valid, synthetic=True)

    run(scenario())


def test_false_validator_is_not_a_grant_to_call_native_protocol():
    async def rejected():
        return False

    def forbidden(request):
        raise AssertionError("Non-authorizing callback reached native protocol")

    with pytest.raises(ValueError, match="VOICE_AUTHORIZATION_INVALID"):
        run(fetch(check=rejected, action=forbidden))


def test_real_native_wav_conversion_and_independent_core_proof_validation(tmp_path):
    result = run(prepare(tmp_path, wav(rate=48000, channels=2)))
    assert result.sample_rate == 16000 and result.channels == 1 and result.frames == 16000
    assert result.duration_seconds == 1.0 and result.source_partial is False
    assert voice.wav_info(result.body)["audio_sha256"] == result.sha256
    parsed = VoiceASRProof.model_validate(result.proof)
    encoded = base64.urlsafe_b64decode(result.proof_header)
    assert encoded == canonical_bytes(parsed) == result.proof_json.encode()
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(KEY))
    key.public_key().verify(bytes.fromhex(parsed.signature), UPLOAD_DOMAIN + canonical_bytes(
        parsed.model_dump(mode="json", exclude={"signature"})))
    assert parsed.event_id == event()["event_id"] and parsed.source_segment_index == 0
    assert_clean(tmp_path)


@pytest.mark.parametrize("marker", [2, 3])
def test_real_silk_decoder_produces_pcm_and_keeps_conservative_partial_coverage(tmp_path, marker):
    encoded = BytesIO()
    pysilk.encode(BytesIO(b"\x01\x00" * 16000), encoded, 16000, 24000)
    data = bytes([marker]) + encoded.getvalue()[1:]
    result = run(prepare(tmp_path, data))
    assert result.frames == 16000 and result.duration_seconds == 1.0
    assert result.source_partial is True and result.proof["source_partial"] is True
    assert_clean(tmp_path)


@pytest.mark.parametrize("kind", ["seconds", "bytes"])
def test_complete_source_is_truncated_only_at_pcm_frame_boundary_and_marked_partial(tmp_path, kind):
    limits = (replace(voice.DEFAULT_LIMITS, max_seconds=1) if kind == "seconds"
              else replace(voice.DEFAULT_LIMITS, max_wav_bytes=10000))
    result = run(prepare(tmp_path, wav(2), limits=limits))
    assert len(result.body) <= limits.max_wav_bytes and result.source_partial is True
    assert result.frames == (16000 if kind == "seconds" else (10000 - 44) // 2)
    assert result.proof["source_partial"] is True
    assert_clean(tmp_path)


@pytest.mark.parametrize("data", [b"#!SILK_V3", b"\x02#!SILK_V3\x01", b"\x02#!SILK_V3\xff\xff",
                                   b"\x02#!SILK_V3\x05\x00ab", b"#EXTM3U\nhttps://private/secret"])
def test_malformed_or_external_media_never_creates_a_signed_asr_upload(tmp_path, data):
    with pytest.raises(ValueError, match="VOICE_"):
        run(prepare(tmp_path, data))
    assert_clean(tmp_path)


def test_source_revocation_during_native_child_kills_process_and_discards_audio(
    tmp_path, monkeypatch,
):
    processes, checks = [], 0
    create = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        return process

    async def revoke():
        nonlocal checks
        checks += 1
        if checks >= 3:
            raise ValueError("VOICE_SOURCE_RECALLED")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    with pytest.raises(ValueError, match="VOICE_SOURCE_RECALLED"):
        run(prepare(tmp_path, check=revoke))
    assert len(processes) == 1 and processes[0].returncode is not None
    assert_clean(tmp_path)


def test_native_timeout_terminates_child_and_returns_no_audio(tmp_path, monkeypatch):
    processes = []
    create = asyncio.create_subprocess_exec

    async def hanging(*args, **kwargs):
        process = await create(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", hanging)
    with pytest.raises(ValueError, match="VOICE_DECODE_TIMEOUT"):
        run(prepare(tmp_path, limits=replace(voice.DEFAULT_LIMITS, timeout_seconds=1)))
    assert len(processes) == 1 and processes[0].returncode is not None
    assert_clean(tmp_path)


def test_child_exit_race_does_not_replace_original_cancellation_error():
    class Exited:
        returncode = None

        def terminate(self):
            raise ProcessLookupError()

        async def wait(self):
            self.returncode = 0
            return 0

    process = Exited()
    run(voice._stop(process))
    assert process.returncode == 0


def test_last_source_check_runs_after_private_temporary_files_are_removed(tmp_path):
    entered_native = False

    async def final_recall():
        nonlocal entered_native
        work = tmp_path / "owned-work"
        if work.exists() and list(work.iterdir()):
            entered_native = True
        if entered_native and list(work.iterdir()) == []:
            raise ValueError("VOICE_SOURCE_RECALLED")

    with pytest.raises(ValueError, match="VOICE_SOURCE_RECALLED"):
        run(prepare(tmp_path, check=final_recall))
    assert_clean(tmp_path)


def delivery():
    raw = wav()
    audio = VoiceAudioResult(
        audio_base64=base64.b64encode(raw).decode(), audio_sha256=hashlib.sha256(raw).hexdigest(),
        sample_rate=16000, channels=1, frames=16000, duration_seconds=1.0,
    ).model_dump(mode="json")
    message_id, event_id = "msg_" + "1" * 32, event()["event_id"]
    item = {"message_id": message_id, "lease_token": "lease_" + "2" * 32,
            "lease_until": (NOW + timedelta(seconds=60)).isoformat(),
            "delivery_key": "reply:" + event_id + ":v1", "action_type": "qq.private.reply",
            "conversation_platform_session_key": config().origin,
            "content": {"text": "a verified private reply"}, "reply_revision": 3,
            "authority": AUTHORITY, "origin_event_id": event_id}
    binding = {
        "schema_version": 3,
        "retirement_token": "random-proof-" + "x" * 32,
        "job_id": "voice_" + "3" * 32,
        "generation": 1,
        "message_id": message_id,
        "delivery_key": item["delivery_key"],
        "event_id": event_id,
        "authority": AUTHORITY,
        "source_sha256": "4" * 64,
        "identity_sha256": voice.digest([config().owner_qq_id, config().bot_qq_id]),
        "privacy_epoch": 1,
        "reply_revision": 3,
        "node_id": "local_voice",
        "manifest_sha256": "5" * 64,
        "node_config_sha256": "6" * 64,
        "voice_policy_sha256": POLICY,
        "content_sha256": voice.digest(item["content"]),
        "reply_sha256": voice.digest(item["content"]["text"]),
        "result_sha256": voice.digest(audio),
        "audio_sha256": audio["audio_sha256"],
        "audio_bytes": len(raw),
        "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
    }
    # Independently authored Core DTO produces the actual outbound serialization.
    bound = VoiceDeliveryBinding.model_validate(binding)
    envelope = VoiceDeliveryEnvelope(binding=bound, binding_sha256=voice.digest(
        bound.model_dump(mode="json")), audio=audio)
    item["voice"] = envelope.model_dump(mode="json")
    return item


def prepared_delivery(item):
    return voice.prepare_delivery(item, config(), authority=AUTHORITY,
                                  voice_policy_sha256=POLICY, now=NOW)


def test_outbound_record_uses_verified_inline_pcm_and_exact_final_delivery_check():
    item = delivery()
    envelope = VoiceDeliveryEnvelope.model_validate(item["voice"])
    assert canonical_bytes(VoiceDeliveryEnvelope.model_validate_json(
        envelope.model_dump_json())) == canonical_bytes(envelope)
    prepared = prepared_delivery(item)
    assert prepared.message == [{"type": "record", "data": {
        "file": "base64://" + item["voice"]["audio"]["audio_base64"],
    }}]
    binding = item["voice"]["binding"]
    assert prepared.check == {"binding_sha256": item["voice"]["binding_sha256"],
        "job_id": binding["job_id"], "generation": binding["generation"],
        "result_sha256": binding["result_sha256"], "audio_sha256": binding["audio_sha256"],
        "audio_bytes": binding["audio_bytes"], "retirement_token": binding["retirement_token"]}
    assert prepared.body_sha256 == voice.digest({
        "user_id": int(config().owner_qq_id), "message": prepared.message, "auto_escape": True,
    })
    prepared.message[0]["data"]["file"] = "file:///private"
    assert prepared.message[0]["data"]["file"].startswith("base64://")


def test_historical_voice_binding_is_readable_but_not_a_native_send_envelope():
    from companion.voice_protocol import LegacyVoiceDeliveryBinding

    item = delivery()
    item.pop("authority")
    item.pop("origin_event_id")
    binding = item["voice"]["binding"]
    binding.pop("authority")
    binding.update(schema_version=2, window_sha256="c" * 64)
    original = LegacyVoiceDeliveryBinding.model_validate(binding)
    item["voice"]["binding_sha256"] = voice.digest(original.model_dump(mode="json"))
    prepared = voice.prepare_delivery(item, config(), plan_sha256="c" * 64,
                                      voice_policy_sha256=POLICY, now=NOW)
    assert prepared.check["binding_sha256"] == item["voice"]["binding_sha256"]
    with pytest.raises(ValueError):
        voice.prepare_delivery(item, config(), authority=AUTHORITY,
                               voice_policy_sha256=POLICY, now=NOW)


@pytest.mark.parametrize("field,value", [
    ("message_id", "msg_" + "9" * 32), ("authority", AUTHORITY | {"revision": 2}),
    ("schema_version", 2), ("window_sha256", "9" * 64),
    ("voice_policy_sha256", "9" * 64), ("identity_sha256", "9" * 64),
    ("reply_revision", 4), ("content_sha256", "9" * 64), ("reply_sha256", "9" * 64),
    ("audio_sha256", "9" * 64), ("audio_bytes", 44), ("generation", True),
    ("expires_at", NOW.isoformat()),
])
def test_audio_binding_rejects_source_authority_identity_content_or_result_drift(field, value):
    item = delivery()
    item["voice"]["binding"][field] = value
    item["voice"]["binding_sha256"] = voice.digest(item["voice"]["binding"])
    with pytest.raises(ValueError):
        prepared_delivery(item)


def test_voice_cannot_drop_an_existing_image_or_become_a_new_text_attempt():
    item = delivery()
    item["content"]["image"] = {"source": "catalog"}
    with pytest.raises(ValueError, match="VOICE_DELIVERY_CHANGED"):
        prepared_delivery(item)
    assert "fallback" not in voice.PreparedVoiceDelivery.__dataclass_fields__


def test_voice_helper_imports_and_validates_without_core_or_pydantic(tmp_path):
    root = Path(__file__).resolve().parents[1]
    isolated = tmp_path / "isolated"
    for package in ("companion_onebot_bridge", "companion_meme_protocol",
                    "companion_delivery_protocol"):
        shutil.copytree(root / package, isolated / package,
                        ignore=shutil.ignore_patterns("__pycache__"))
    script = """
import importlib.abc, sys
sys.path.insert(0, sys.argv[1])
class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'companion', 'fastapi', 'pydantic', 'alembic'}:
            raise ImportError('FORBIDDEN_CORE_DEPENDENCY:' + fullname)
sys.meta_path.insert(0, Boundary())
from companion_onebot_bridge.voice_media import original_records
assert len(original_records([{'type':'record','data':{'file':'real-cache.amr'}}])) == 1
print('isolated voice helper loaded')
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(isolated)],
                            cwd=isolated, env=os.environ.copy(), capture_output=True,
                            text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
