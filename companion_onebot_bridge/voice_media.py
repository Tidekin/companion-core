"""Bounded native-record custody and exact voice delivery payloads, independent of Core.

SnowLuma db142db9e6398c779283eab9afc3552da1e2c361: actions/extended.ts get_record,
modules/media-actions.ts getRecordInfo, element-codecs.ts record, and protocol
highway/ptt-upload.ts establish cache-id lookup and base64:// WAV send semantics.
No out_format is requested: that delegates download/transcode limits to SnowLuma.

SILK uses silk-python 0.2.8 (BSD package metadata), synodriver/pysilk commit
b0fbba74506bb8be23ef5e120b983bbb2f70ac19, only inside the resource-limited child.
Its public decoder does not expose all internal packet-frame coverage, so SILK
input is conservatively partial. No codec implementation is copied here.

The caller owns authenticated capture, durable admission and final send authority.
These helpers never upload to Core, send messages, retry, or fall back to text.
"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import math
import os
import re
import sys
import tempfile
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion_delivery_protocol import VOICE_CHECK_FIELDS

from .event_normalizer import decode, normalize
from .files import _destination, _get, _opaque
from .images import _private_download, global_address, resolve_public
from .network import checked_http_client, private_request_matches
from .onebot_client import bounded_post
from .reply_segments import event_id as delivery_event_id
from .rich_input import input_digest

UPLOAD_DOMAIN = b"companion-core/onebot-owner-voice/v1\x00"
ACTION = "/get_record"
PACKAGE_ROOT = str(Path(__file__).resolve().parents[1])
MAX_AUDIO_BYTES = 8_000_000
DOWNLOAD_TIMEOUT_SECONDS = 10
MIMES = frozenset({
    "application/octet-stream", "audio/wav", "audio/x-wav", "audio/amr", "audio/amr-wb",
    "audio/silk", "audio/x-silk", "audio/mpeg", "audio/mp3", "audio/ogg", "application/ogg",
    "audio/flac", "audio/x-flac", "audio/mp4", "audio/webm",
})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("VOICE_DIGEST_INVALID")
    return value


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise ValueError("VOICE_FIELDS_INVALID")


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("VOICE_LIMIT_INVALID")


def _instant(value):
    if not isinstance(value, str):
        raise ValueError("VOICE_TIMESTAMP_INVALID")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("VOICE_TIMESTAMP_INVALID")
    return stamp


@dataclass(frozen=True)
class AudioLimits:
    max_source_bytes: int = MAX_AUDIO_BYTES
    max_wav_bytes: int = MAX_AUDIO_BYTES
    max_seconds: int = 120
    sample_rate: int = 16000
    timeout_seconds: int = 15
    max_memory_bytes: int = 384 * 1024 * 1024

    def __post_init__(self):
        _integer(self.max_source_bytes, 44, MAX_AUDIO_BYTES)
        _integer(self.max_wav_bytes, 46, MAX_AUDIO_BYTES)
        _integer(self.max_seconds, 1, 120)
        _integer(self.timeout_seconds, 1, 30)
        _integer(self.max_memory_bytes, 128 * 1024 * 1024, 512 * 1024 * 1024)
        if type(self.sample_rate) is not int or self.sample_rate != 16000:
            raise ValueError("VOICE_SAMPLE_RATE_INVALID")


DEFAULT_LIMITS = AudioLimits()


@dataclass(frozen=True, repr=False)
class OriginalRecord:
    index: int
    slot: int
    source_sha256: str
    file: str
    source_url: str = ""

    def __post_init__(self):
        _integer(self.index, 0, 63)
        _integer(self.slot, 0, self.index)
        _sha(self.source_sha256)
        if (not _opaque(self.file, 512) or self.file in {".", ".."}
            or any(char in self.file for char in "\\/:")):
            raise ValueError("VOICE_CACHE_ID_REQUIRED")
        if (not isinstance(self.source_url, str) or len(self.source_url) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in self.source_url)):
            raise ValueError("VOICE_SOURCE_URL_INVALID")


@dataclass(frozen=True, repr=False)
class DownloadedRecord:
    original: OriginalRecord
    body: bytes
    mime: str
    sha256: str

    def __post_init__(self):
        if (not isinstance(self.original, OriginalRecord) or type(self.body) is not bytes
            or not 1 <= len(self.body) <= MAX_AUDIO_BYTES or self.mime not in MIMES
            or hashlib.sha256(self.body).hexdigest() != self.sha256):
            raise ValueError("VOICE_INPUT_CHANGED")


@dataclass(frozen=True, repr=False)
class AuthenticatedASRSource:
    """Capture-derived binding; the caller's live check remains the authority.

    Build only after verifying the original signed capture manifest, its input
    digest, owner/private identity, and exact original record. This allows the
    durable queue to discard plaintext without constructing a replacement event.
    """

    event_id: str
    source_event_id: str
    source_input_sha256: str
    source_segment_index: int
    source_record_sha256: str

    def __post_init__(self):
        if (not isinstance(self.event_id, str)
            or not re.fullmatch(r"evt_[a-f0-9]{64}", self.event_id)
            or not isinstance(self.source_event_id, str)
            or not 1 <= len(self.source_event_id) <= 256
            or any(ord(char) < 32 or ord(char) == 127 for char in self.source_event_id)):
            raise ValueError("VOICE_ASR_SOURCE_INVALID")
        _sha(self.source_input_sha256)
        _sha(self.source_record_sha256)
        _integer(self.source_segment_index, 0, 63)


@dataclass(frozen=True, repr=False)
class PreparedVoiceInput:
    original: OriginalRecord
    body: bytes
    sha256: str
    sample_rate: int
    channels: int
    frames: int
    duration_seconds: float
    source_partial: bool
    proof_json: str
    proof_header: str
    mime_type: str = "audio/wav"

    @property
    def proof(self):
        return json.loads(self.proof_json)


@dataclass(frozen=True, repr=False)
class PreparedVoiceDelivery:
    binding_sha256: str
    content_sha256: str
    body_sha256: str
    message_json: str = field(repr=False)
    check_json: str = field(repr=False)

    @property
    def message(self):
        return json.loads(self.message_json)

    @property
    def check(self):
        return json.loads(self.check_json)


def original_records(message):
    """Only top-level record segments from a previously authenticated owner capture."""
    if isinstance(message, str):
        return ()
    if not isinstance(message, list) or not 1 <= len(message) <= 64:
        raise ValueError("VOICE_MESSAGE_INVALID")
    records = []
    for index, segment in enumerate(message):
        if not isinstance(segment, dict) or segment.get("type") != "record":
            continue
        _fields(segment, {"type", "data"})
        data = segment["data"]
        if (not isinstance(data, dict) or not {"file"} <= set(data) <= {"file", "url"}
            or "url" in data and (not isinstance(data["url"], str) or len(data["url"]) > 4096)):
            raise ValueError("VOICE_SEGMENT_INVALID")
        records.append(OriginalRecord(index, len(records), digest(segment), data["file"],
                                      data.get("url", "")))
    return tuple(records)


def owner_records(frame, config, *, now=None):
    if normalize(frame, config, now=now, rich_input_scope="references_v1") is None:
        return ()
    return original_records(decode(frame, config.max_frame_bytes, max_depth=20)["message"])


async def _record_info(original, onebot, check, synthetic):
    body, config = {"file": original.file}, onebot.config

    async def boundary(request):
        await check()
        content = request.content
        if (request.method != "POST"
            or not private_request_matches(request, config.onebot_http_url, ACTION,
                                           synthetic=synthetic)
            or request.headers.get("Authorization") != "Bearer " + config.onebot_token
            or "Cookie" in request.headers or "Transfer-Encoding" in request.headers
            or decode(content, 4096) != body):
            raise ValueError("VOICE_ACTION_CHANGED")
        request.stream = httpx.ByteStream(content)
        request.headers["Content-Length"] = str(len(content))

    await check()
    async with checked_http_client(onebot.client, boundary) as client:
        result = await bounded_post(client, ACTION, body)
    await check()
    if (result.get("status") != "ok" or type(result.get("retcode")) is not int
        or result["retcode"] != 0 or not isinstance(result.get("data"), dict)):
        raise ValueError("VOICE_URL_RESOLUTION_FAILED")
    value = result["data"]
    _fields(value, {"file", "url", "file_size", "file_name"})
    if (value["file"] != value["url"] or not isinstance(value["file_name"], str)
        or not 1 <= len(value["file_name"]) <= 512
        or not isinstance(value["file_size"], str)
        or not re.fullmatch(r"0|[1-9][0-9]{0,15}", value["file_size"])):
        raise ValueError("VOICE_URL_RESOLUTION_FAILED")
    # SnowLuma's filename aliases are mutable across messages. A refreshed or
    # different URL cannot establish this captured record's identity by itself.
    if not original.source_url or value["url"] != original.source_url:
        raise ValueError("VOICE_RECORD_SOURCE_CHANGED")
    return value["url"], int(value["file_size"])


async def fetch_record(original, max_bytes=MAX_AUDIO_BYTES, *, onebot, check, transport=None,
                       resolver=resolve_public, synthetic=False, allowed_hosts=None,
                       allowed_ports=None):
    """One cache lookup and one pinned public GET; never use a message URL or local path."""
    if (not isinstance(original, OriginalRecord) or not callable(check)
        or type(synthetic) is not bool):
        raise ValueError("VOICE_DOWNLOAD_GRANT_INVALID")
    _integer(max_bytes, 1, MAX_AUDIO_BYTES)
    guard_error = None
    guard_active = False

    async def guard():
        nonlocal guard_error, guard_active
        guard_active = True
        try:
            if await check() is not None:
                raise ValueError("VOICE_AUTHORIZATION_INVALID")
        except Exception as error:
            guard_error = error
            raise
        else:
            guard_active = False

    delegate = transport or httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    response, closed = None, False
    quiet = _private_download.set(True)
    try:
        async with asyncio.timeout(DOWNLOAD_TIMEOUT_SECONDS):
            locator, native_size = await _record_info(original, onebot, guard, synthetic)
            if native_size > max_bytes:
                raise ValueError("VOICE_SOURCE_TOO_LARGE")
            target = _destination(locator, allowed_hosts, allowed_ports)
            try:
                ipaddress.ip_address(target.host)
                addresses = [target.host]
            except ValueError:
                addresses = await resolver(target.host, target.port or (
                    443 if target.scheme == "https" else 80))
            if not addresses or any(not global_address(address) for address in addresses):
                raise ValueError("VOICE_NONPUBLIC_ADDRESS")
            response = await _get(delegate, target, addresses[0], guard)
            await guard()
            if response.status_code != 200 or "content-range" in response.headers:
                raise ValueError("VOICE_DOWNLOAD_REJECTED")
            if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                raise ValueError("VOICE_ENCODING_UNSUPPORTED")
            mime = response.headers.get("content-type", "application/octet-stream")
            mime = mime.split(";", 1)[0].strip().lower()
            if mime not in MIMES:
                raise ValueError("VOICE_MIME_UNSUPPORTED")
            declared = response.headers.get("content-length")
            if declared is not None and (not re.fullmatch(r"[1-9][0-9]{0,15}", declared)
                                         or int(declared) > max_bytes):
                raise ValueError("VOICE_SOURCE_TOO_LARGE")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(chunk) > max_bytes - len(body):
                    raise ValueError("VOICE_SOURCE_TOO_LARGE")
                await guard()
                body.extend(chunk)
            await response.aclose()
            response = None
            if transport is None:
                await delegate.aclose()
                closed = True
            await guard()
            if (not body or declared is not None and len(body) != int(declared)
                or native_size and len(body) != native_size):
                raise ValueError("VOICE_SOURCE_INCOMPLETE")
            data = bytes(body)
            return DownloadedRecord(original, data, mime, hashlib.sha256(data).hexdigest())
    except (httpx.HTTPError, OSError) as error:
        if guard_error is not None:
            raise guard_error from None
        if guard_active:
            from .capture import capture_diagnostic

            error.capture_diagnostic = capture_diagnostic(error, "core_check")
            raise
        code = ("VOICE_FETCH_TIMEOUT" if isinstance(error, (TimeoutError, httpx.TimeoutException))
                else "VOICE_FETCH_FAILED")
        raise ValueError(code) from None
    finally:
        try:
            if response is not None:
                await response.aclose()
            if transport is None and not closed:
                await delegate.aclose()
        finally:
            _private_download.reset(quiet)


def wav_info(data, *, max_bytes=MAX_AUDIO_BYTES, max_seconds=120):
    """Validate complete PCM16 RIFF/WAVE without a native decoder or Core imports."""
    if (type(data) is not bytes or not 44 <= len(data) <= max_bytes
        or data[:4] != b"RIFF" or data[8:12] != b"WAVE"
        or int.from_bytes(data[4:8], "little") != len(data) - 8):
        raise ValueError("VOICE_WAV_INVALID")
    offset, fmt, size = 12, None, None
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("VOICE_WAV_INVALID")
        kind, count = data[offset:offset + 4], int.from_bytes(data[offset + 4:offset + 8], "little")
        offset += 8
        if offset + count + count % 2 > len(data):
            raise ValueError("VOICE_WAV_INVALID")
        if kind == b"fmt ":
            if fmt is not None or count < 16:
                raise ValueError("VOICE_WAV_INVALID")
            fmt = data[offset:offset + 16]
        elif kind == b"data":
            if fmt is None or size is not None:
                raise ValueError("VOICE_WAV_INVALID")
            size = count
        offset += count + count % 2
    if fmt is None or size is None:
        raise ValueError("VOICE_WAV_INVALID")
    channels, rate = int.from_bytes(fmt[2:4], "little"), int.from_bytes(fmt[4:8], "little")
    if (int.from_bytes(fmt[:2], "little") != 1 or int.from_bytes(fmt[14:16], "little") != 16
        or channels not in {1, 2} or not 8000 <= rate <= 96000
        or int.from_bytes(fmt[12:14], "little") != channels * 2
        or int.from_bytes(fmt[8:12], "little") != rate * channels * 2
        or size == 0 or size % (channels * 2)):
        raise ValueError("VOICE_WAV_INVALID")
    frames = size // (channels * 2)
    if frames / rate > max_seconds:
        raise ValueError("VOICE_WAV_TOO_LONG")
    try:
        with wave.open(BytesIO(data), "rb") as source:
            if (source.getnchannels() != channels or source.getframerate() != rate
                or source.getsampwidth() != 2 or source.getnframes() != frames
                or len(source.readframes(frames + 1)) != size):
                raise ValueError("VOICE_WAV_INVALID")
    except (wave.Error, EOFError):
        raise ValueError("VOICE_WAV_INVALID") from None
    return {"sample_rate": rate, "channels": channels, "frames": frames,
            "duration_seconds": frames / rate, "audio_bytes": len(data),
            "audio_sha256": hashlib.sha256(data).hexdigest()}


def prepare_delivery(item, config, *, voice_policy_sha256, now, authority=None, plan_sha256=None):
    """Freeze one native record send. Final delivery validation uses ``prepared.check``."""
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("VOICE_TIMESTAMP_INVALID")
    if (authority is None) == (plan_sha256 is None):
        raise ValueError("VOICE_SCOPE_REQUIRED")
    native = authority is not None
    if native:
        from companion_delivery_protocol.authority import validate_authority_ref

        scope = {"authority": validate_authority_ref(authority)}
        if ("window_sha256" in item
            or validate_authority_ref(item.get("authority")) != scope["authority"]):
            raise ValueError("VOICE_DELIVERY_CHANGED")
    else:
        scope = {"window_sha256": _sha(plan_sha256)}
    envelope = item.get("voice")
    is_song = (isinstance(envelope, dict) and isinstance(envelope.get("audio"), dict)
               and envelope["audio"].get("kind") == "sing")
    if is_song and not native:
        raise ValueError("VOICE_DELIVERY_CHANGED")
    _fields(envelope, {"binding", "binding_sha256", "audio"}
            | ({"singing_manifest"} if is_song else set()))
    binding, audio = envelope["binding"], envelope["audio"]
    hashes = {"source_sha256", "identity_sha256", "manifest_sha256",
              "node_config_sha256", "voice_policy_sha256", "content_sha256", "reply_sha256",
              "result_sha256", "audio_sha256"}
    _fields(binding, hashes | scope.keys() | {"schema_version", "job_id", "generation",
        "message_id", "delivery_key", "event_id", "privacy_epoch", "reply_revision", "node_id",
        "audio_bytes", "expires_at", "retirement_token"}
        | ({"singing_segment_index"} if is_song else set()))
    for key in hashes:
        _sha(binding[key])
    if not isinstance(binding["retirement_token"], str) or not re.fullmatch(
        r"[a-zA-Z0-9_-]{32,256}", binding["retirement_token"]):
        raise ValueError("VOICE_BINDING_INVALID")
    version = 3 if native else 2
    for key, low, high in (("schema_version", version, version), ("generation", 1, 2**63 - 1),
                           ("privacy_epoch", 0, 2**63 - 1), ("reply_revision", 0, 2**63 - 1),
                           ("audio_bytes", 44, 4_000_000)):
        _integer(binding[key], low, high)
    for key, pattern in (("job_id", r"voice_[a-f0-9]{32}"), ("message_id", r"msg_[a-f0-9]{32}"),
                         ("node_id", r"[A-Za-z0-9_-]{1,64}")):
        if not isinstance(binding[key], str) or not re.fullmatch(pattern, binding[key]):
            raise ValueError("VOICE_BINDING_INVALID")
    if "reply_revision" in item:
        _integer(item["reply_revision"], 0, 2**63 - 1)
    if not isinstance(binding["delivery_key"], str) or not 1 <= len(binding["delivery_key"]) <= 512:
        raise ValueError("VOICE_BINDING_INVALID")
    if (item.get("action_type") not in {"qq.private.reply", "qq.private.proactive"}
        or item.get("delivery_lane", "owner_private") != "owner_private"
        or item.get("conversation_platform_session_key") != config.origin
        or not isinstance(item.get("content"), dict)
        or set(item["content"]) != {"text"}
        or not isinstance(item["content"].get("text"), str)
        or not 1 <= len(item["content"]["text"]) <= 20000
        or binding["message_id"] != item.get("message_id")
        or binding["delivery_key"] != item.get("delivery_key")
        or binding["event_id"] != delivery_event_id(item)
        or any(binding[key] != value for key, value in scope.items())
        or binding["voice_policy_sha256"] != _sha(voice_policy_sha256)
        or binding["identity_sha256"] != digest([config.owner_qq_id, config.bot_qq_id])
        or binding["content_sha256"] != digest(item["content"])
        or binding["reply_sha256"] != digest(item["content"]["text"])
        or "reply_revision" in item and binding["reply_revision"] != item["reply_revision"]
        or _instant(binding["expires_at"]) <= now or _instant(item["lease_until"]) <= now
        or envelope["binding_sha256"] != digest(binding)):
        raise ValueError("VOICE_DELIVERY_CHANGED")
    _fields(audio, {"kind", "audio_base64", "audio_sha256", "sample_rate", "channels",
                    "sample_width", "frames", "duration_seconds", "mime_type"})
    encoded = audio["audio_base64"]
    if (audio["kind"] != ("sing" if is_song else "tts") or audio["mime_type"] != "audio/wav"
        or not isinstance(encoded, str) or not 60 <= len(encoded) <= 4 * ((4_000_000 + 2) // 3)):
        raise ValueError("VOICE_AUDIO_INVALID")
    for key, low, high in (("sample_width", 2, 2), ("channels", 1, 2),
                           ("sample_rate", 8000, 96000), ("frames", 1, 5_760_000)):
        _integer(audio[key], low, high)
    if (type(audio["duration_seconds"]) is not float
        or not math.isfinite(audio["duration_seconds"]) or not 0 < audio["duration_seconds"] <= 60):
        raise ValueError("VOICE_AUDIO_INVALID")
    data = base64.b64decode(encoded, validate=True)
    actual = wav_info(data, max_bytes=4_000_000, max_seconds=60)
    if is_song:
        from companion_delivery_protocol.singing import validate_manifest, validate_part

        manifest = validate_manifest(envelope["singing_manifest"])
        validate_part(manifest, binding["singing_segment_index"],
                      {**actual, "audio_bytes": len(data), "sample_width": 2})
        if manifest["node_manifest_sha256"] != binding["manifest_sha256"]:
            raise ValueError("VOICE_AUDIO_INVALID")
        result_sha = digest(manifest)
    else:
        result_sha = digest(audio)
    if (base64.b64encode(data).decode() != encoded or binding["result_sha256"] != result_sha
        or binding["audio_sha256"] != audio["audio_sha256"]
        or binding["audio_bytes"] != len(data)
        or any(audio[key] != actual[key] for key in (
            "sample_rate", "channels", "frames", "duration_seconds", "audio_sha256"))):
        raise ValueError("VOICE_AUDIO_INVALID")
    message = [{"type": "record", "data": {"file": "base64://" + encoded}}]
    check = {"binding_sha256": envelope["binding_sha256"],
             **{key: binding[key] for key in VOICE_CHECK_FIELDS - {"binding_sha256"}}}
    return PreparedVoiceDelivery(
        envelope["binding_sha256"], binding["content_sha256"],
        digest({"user_id": int(config.owner_qq_id), "message": message, "auto_escape": True}),
        canonical(message).decode(), canonical(check).decode(),
    )


async def _validate(check, cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise ValueError("VOICE_INPUT_CANCELLED")
    if await check() is not None:
        raise ValueError("VOICE_AUTHORIZATION_INVALID")
    if cancel_event is not None and cancel_event.is_set():
        raise ValueError("VOICE_INPUT_CANCELLED")


async def _stop(process):
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 0.5)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def _pipe(stream, overflow):
    body = bytearray()
    while chunk := await stream.read(4096):
        if len(body) + len(chunk) > 8192:
            overflow.set()
        else:
            body.extend(chunk)
    return bytes(body)


async def _process(request, limits, check, cancel_event):
    await _validate(check, cancel_event)
    environment = {key: value for key, value in os.environ.items() if key.upper() in {
        "SYSTEMROOT", "WINDIR", "PATH", "COMSPEC", "PATHEXT", "LANG", "LC_ALL",
    }}
    environment.update(PYTHONPATH=PACKAGE_ROOT, PYTHONDONTWRITEBYTECODE="1",
                       PYTHONIOENCODING="utf-8", TMPDIR=str(request.parent),
                       TEMP=str(request.parent), TMP=str(request.parent))
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-B", "-m", "companion_onebot_bridge.voice_media", "--decode", str(request),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=request.parent,
        env=environment,
        **({"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}),
    )
    overflow = asyncio.Event()
    stdout = asyncio.create_task(_pipe(process.stdout, overflow))
    stderr = asyncio.create_task(_pipe(process.stderr, overflow))
    waiter = asyncio.create_task(process.wait())
    try:
        while not waiter.done():
            await asyncio.wait({waiter}, timeout=0.1)
            await _validate(check, cancel_event)
            if overflow.is_set():
                raise ValueError("VOICE_DECODE_RESPONSE_LIMIT")
        raw, _ = await asyncio.gather(stdout, stderr)
        if overflow.is_set() or process.returncode != 0:
            raise ValueError("VOICE_DECODE_FAILED")
        result = decode(raw, 8192)
        if "error" in result:
            _fields(result, {"error"})
            code = result["error"]
            if not isinstance(code, str) or not re.fullmatch(r"VOICE_[A-Z_]{1,56}", code):
                code = "VOICE_DECODE_FAILED"
            raise ValueError(code)
        return result
    finally:
        await _stop(process)
        await asyncio.gather(stdout, stderr, waiter, return_exceptions=True)


def _asr_source(event, original):
    if type(event) is AuthenticatedASRSource:
        if (event.source_segment_index != original.index
            or event.source_record_sha256 != original.source_sha256):
            raise ValueError("VOICE_RECORD_SOURCE_REQUIRED")
        return event
    if (not isinstance(event, dict)
        or event.get("source") != "qq.onebot" or event.get("event_type") != "message.received"
        or event.get("conversation", {}).get("scope") != "private"
        or event.get("actor", {}).get("role") != "owner"
        or not isinstance(event.get("event_id"), str)
        or not re.fullmatch(r"evt_[a-f0-9]{64}", event["event_id"])
        or not isinstance(event.get("source_event_id"), str)
        or not 1 <= len(event["source_event_id"]) <= 256):
        raise ValueError("VOICE_ASR_SOURCE_INVALID")
    body = event["payload"]
    segment = next((segment for segment in (body.get("rich") or {}).get("segments", [])
                    if segment.get("index") == original.index), None)
    if segment != {"index": original.index, "type": "record",
                   "actor": "UNVERIFIED", "coverage": "UNSUPPORTED"}:
        raise ValueError("VOICE_RECORD_SOURCE_REQUIRED")
    return AuthenticatedASRSource(event["event_id"], event["source_event_id"], input_digest(body),
                                  original.index, original.source_sha256)


async def prepare_asr(downloaded, event, *, capture_signing_key, check, work_dir,
                      plan_sha256=None, authority=None,
                      limits=DEFAULT_LIMITS, cancel_event=None):
    """Produce a signed upload from an admitted event or AuthenticatedASRSource.

    ``check`` must revalidate the exact captured source, current reply/input cap,
    recall/erasure status and active window. It is called throughout native work
    and after all private temporary files are removed, immediately before return.
    A source binding alone never authorizes processing.
    """
    if (not isinstance(downloaded, DownloadedRecord) or not isinstance(limits, AudioLimits)
        or not callable(check) or not isinstance(capture_signing_key, str)
        or not re.fullmatch(r"[a-f0-9]{64}", capture_signing_key)):
        raise ValueError("VOICE_ASR_SOURCE_INVALID")
    if (authority is None) == (plan_sha256 is None):
        raise ValueError("VOICE_SCOPE_REQUIRED")
    if authority is not None:
        from companion_delivery_protocol.authority import validate_authority_ref

        scope = {"authority": validate_authority_ref(authority)}
        domain = b"companion-core/onebot-owner-voice/v2\x00"
    else:
        scope = {"plan_sha256": _sha(plan_sha256)}
        domain = UPLOAD_DOMAIN
    source = _asr_source(event, downloaded.original)
    if len(downloaded.body) > limits.max_source_bytes:
        raise ValueError("VOICE_SOURCE_TOO_LARGE")
    root = Path(work_dir)
    if not root.is_absolute():
        raise ValueError("VOICE_WORK_DIRECTORY_INVALID")
    resolved = await asyncio.to_thread(root.resolve)
    if resolved != root or await asyncio.to_thread(root.is_symlink):
        raise ValueError("VOICE_WORK_DIRECTORY_INVALID")
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    await _validate(check, cancel_event)
    try:
        async with asyncio.timeout(limits.timeout_seconds):
            with tempfile.TemporaryDirectory(prefix="voice-media-", dir=root) as temporary:
                directory = Path(temporary)
                await asyncio.to_thread((directory / "input.media").write_bytes, downloaded.body)
                request = directory / "request.json"
                await asyncio.to_thread(request.write_bytes, canonical({
                    "limits": asdict(limits), "input_bytes": len(downloaded.body),
                    "input_sha256": downloaded.sha256,
                }))
                result = await _process(request, limits, check, cancel_event)
                _fields(result, {"sample_rate", "channels", "frames", "duration_seconds",
                                 "audio_sha256", "audio_bytes", "source_partial"})
                if type(result["source_partial"]) is not bool:
                    raise ValueError("VOICE_DECODE_RESPONSE_INVALID")
                output = directory / "audio.wav"
                info = await asyncio.to_thread(output.stat)
                if (info.st_size > limits.max_wav_bytes
                    or await asyncio.to_thread(output.is_symlink)):
                    raise ValueError("VOICE_DECODE_OUTPUT_LIMIT")
                data = await asyncio.to_thread(output.read_bytes)
                actual = wav_info(data, max_bytes=limits.max_wav_bytes,
                                  max_seconds=limits.max_seconds)
                if (actual["sample_rate"] != limits.sample_rate or actual["channels"] != 1
                    or any(actual[key] != result[key] for key in actual)):
                    raise ValueError("VOICE_DECODE_RESPONSE_INVALID")
    except TimeoutError:
        raise ValueError("VOICE_DECODE_TIMEOUT") from None
    unsigned = {**scope, "event_id": source.event_id,
                "source_event_id": source.source_event_id,
                "source_input_sha256": source.source_input_sha256,
                "source_segment_index": source.source_segment_index,
                "audio_sha256": actual["audio_sha256"], "audio_bytes": len(data),
                "source_partial": result["source_partial"]}
    signature = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(capture_signing_key)).sign(
        domain + canonical(unsigned),
    ).hex()
    proof = canonical({**unsigned, "signature": signature})
    prepared = PreparedVoiceInput(
        downloaded.original, data, actual["audio_sha256"],
        actual["sample_rate"], actual["channels"],
        actual["frames"], actual["duration_seconds"], result["source_partial"], proof.decode(),
        base64.urlsafe_b64encode(proof).decode(),
    )
    await _validate(check, cancel_event)
    return prepared


def _resource_limits(limits):
    """Mandatory native CPU, memory, and process limits before importing decoders."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Basic(ctypes.Structure):
                _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
                    ("maximum_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]

            class IO(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in (
                    "read_operations", "write_operations", "other_operations", "read_bytes",
                    "write_bytes", "other_bytes")]

            class Extended(ctypes.Structure):
                _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t),
                    ("peak_job_memory", ctypes.c_size_t)]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel.CreateJobObjectW.restype = wintypes.HANDLE
            kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
            kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            handle, settings = kernel.CreateJobObjectW(None, None), Extended()
            settings.basic.flags = 0x2 | 0x8 | 0x100 | 0x2000
            settings.basic.process_time = limits.timeout_seconds * 10_000_000
            settings.basic.active_processes = 1
            settings.process_memory = limits.max_memory_bytes
            if (not handle or not kernel.SetInformationJobObject(
                    handle, 9, ctypes.byref(settings), ctypes.sizeof(settings))
                or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess())):
                raise ValueError("VOICE_RESOURCE_LIMIT_UNAVAILABLE")
            return handle
        if sys.platform.startswith("linux"):
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (limits.max_memory_bytes,) * 2)
            resource.setrlimit(resource.RLIMIT_CPU, (limits.timeout_seconds,) * 2)
            resource.setrlimit(resource.RLIMIT_FSIZE, (limits.max_wav_bytes,) * 2)
            resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
            return None
    except (OSError, ValueError, AttributeError):
        pass
    raise ValueError("VOICE_RESOURCE_LIMIT_UNAVAILABLE")


class _PrefixComplete(Exception):
    pass


class _PCMOutput:
    def __init__(self, wav, limits):
        self.wav, self.frames = wav, 0
        self.maximum = min(limits.max_seconds * limits.sample_rate,
                           (limits.max_wav_bytes - 44) // 2)

    def write(self, body):
        if not isinstance(body, bytes) or len(body) % 2:
            raise ValueError("VOICE_DECODE_INVALID")
        count = min(len(body) // 2, self.maximum - self.frames)
        self.wav.writeframesraw(body[:count * 2])
        self.frames += count
        if count * 2 != len(body):
            raise _PrefixComplete()
        return len(body)

    def read(self, *args):
        raise ValueError("VOICE_OUTPUT_READ_REFUSED")


def _silk_frames(data):
    """Validate SDK packet framing and remove only the standard terminal marker."""
    if data.startswith(b"\x03#!SILK_V3"):
        data = b"\x02" + data[1:]
    offset = 10 if data.startswith(b"\x02#!SILK_V3") else 9
    if data[:offset] not in {b"#!SILK_V3", b"\x02#!SILK_V3"}:
        raise ValueError("VOICE_SILK_INVALID")
    count = 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise ValueError("VOICE_SILK_INVALID")
        size = int.from_bytes(data[offset:offset + 2], "little", signed=True)
        if size == -1 and offset + 2 == len(data) and count:
            return data[:offset]
        if not 1 <= size <= 1250 or offset + 2 + size > len(data):
            raise ValueError("VOICE_SILK_INVALID")
        count += 1
        offset += 2 + size
    if not count:
        raise ValueError("VOICE_SILK_INVALID")
    return data


def _decode_audio(data, output, limits):
    partial = False
    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(limits.sample_rate)
        sink = _PCMOutput(wav, limits)
        if data.startswith((b"#!SILK_V3", b"\x02#!SILK_V3", b"\x03#!SILK_V3")):
            try:
                import pysilk
            except ImportError:
                raise ValueError("VOICE_SILK_DECODER_UNAVAILABLE") from None
            if pysilk.__version__ != "0.2.8":
                raise ValueError("VOICE_SILK_DECODER_UNAVAILABLE")
            partial = True  # Public API does not prove all internal frames were covered.
            try:
                pysilk.decode(BytesIO(_silk_frames(data)), sink, limits.sample_rate)
            except _PrefixComplete:
                pass
        else:
            try:
                import av
            except ImportError:
                raise ValueError("VOICE_DECODER_UNAVAILABLE") from None
            form = (
                "wav" if data.startswith(b"RIFF") and data[8:12] == b"WAVE"
                else "amr" if data.startswith((b"#!AMR\n", b"#!AMR-WB\n"))
                else "ogg" if data.startswith(b"OggS")
                else "flac" if data.startswith(b"fLaC")
                else "mp3" if data.startswith(b"ID3") or (
                    len(data) >= 2 and data[0] == 255 and data[1] & 0xE0 == 0xE0)
                else "mov" if len(data) >= 12 and data[4:8] == b"ftyp"
                else "matroska" if data.startswith(b"\x1aE\xdf\xa3") else None
            )
            if form is None:
                raise ValueError("VOICE_CONTAINER_UNSUPPORTED")

            def no_external(*args):
                raise ValueError("VOICE_EXTERNAL_MEDIA_REFUSED")

            with av.open(BytesIO(data), "r", format=form, io_open=no_external,
                         options={"protocol_whitelist": "", "enable_drefs": "0",
                                  "use_absolute_path": "0"}) as source:
                if len(source.streams) != 1 or len(source.streams.audio) != 1:
                    raise ValueError("VOICE_AUDIO_STREAM_REQUIRED")
                stream = source.streams.audio[0]
                stream.codec_context.thread_count = 1
                if not 8000 <= stream.codec_context.sample_rate <= 96000:
                    raise ValueError("VOICE_SAMPLE_RATE_INVALID")
                converter = av.AudioResampler(format="s16", layout="mono", rate=limits.sample_rate)
                previous_end, decoded = None, 0
                try:
                    for frame in source.decode(stream):
                        decoded += 1
                        if (decoded > 24000 or not 1 <= len(frame.layout.channels) <= 2
                            or not 8000 <= frame.sample_rate <= 96000
                            or not 0 < frame.samples <= 192000):
                            raise ValueError("VOICE_DECODE_LIMIT")
                        if frame.pts is None or frame.time_base is None:
                            partial = True
                        else:
                            at = float(frame.pts * frame.time_base)
                            if not math.isfinite(at) or (
                                previous_end is not None and abs(at - previous_end) > 0.1
                            ):
                                partial = True
                            previous_end = at + frame.samples / frame.sample_rate
                        for pcm in converter.resample(frame):
                            sink.write(bytes(pcm.planes[0])[:pcm.samples * 2])
                    for pcm in converter.resample(None):
                        sink.write(bytes(pcm.planes[0])[:pcm.samples * 2])
                except _PrefixComplete:
                    partial = True
        if not sink.frames:
            raise ValueError("VOICE_AUDIO_EMPTY")
    result = wav_info(output.read_bytes(), max_bytes=limits.max_wav_bytes,
                      max_seconds=limits.max_seconds)
    return {**result, "source_partial": partial}


def _worker(request):
    try:
        value = json.loads(request.read_bytes())
        _fields(value, {"limits", "input_bytes", "input_sha256"})
        limits = AudioLimits(**value["limits"])
        resource_custody = _resource_limits(limits)
        incoming = request.parent / "input.media"
        if incoming.is_symlink() or not incoming.is_file():
            raise ValueError("VOICE_INPUT_CHANGED")
        with incoming.open("rb") as stream:
            data = stream.read(limits.max_source_bytes + 1)
        if (len(data) > limits.max_source_bytes or len(data) != value["input_bytes"]
            or hashlib.sha256(data).hexdigest() != value["input_sha256"]):
            raise ValueError("VOICE_INPUT_CHANGED")
        result = _decode_audio(data, request.parent / "audio.wav", limits)
        del resource_custody
    except ValueError as error:
        code = str(error)
        result = {"error": code if re.fullmatch(r"VOICE_[A-Z_]{1,56}", code)
                  else "VOICE_DECODE_INVALID"}
    except Exception:
        result = {"error": "VOICE_DECODE_INVALID"}
    print(canonical(result).decode())


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--decode":
        raise SystemExit(2)
    _worker(Path(sys.argv[2]))
