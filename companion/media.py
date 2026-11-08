"""Owner-started media sessions: bounded input, consent, cost, and evidence provenance."""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field

from companion import cloud_budget
from companion.contracts import CoreError, StrictModel
from companion.media_provider import HTTPMediaProvider, MediaResult
from companion.observation import ObservationEvent
from companion.policy import ConsentRequest
from companion.privacy import epoch, redact
from companion.store import after, encode, uid

CAPABILITIES = {
    "voice": "audio.transcribe",
    "camera": "camera.observe",
    "android": "android.observe",
}
OPERATIONS = {"audio": "voice", "speech": "voice", "camera": "camera", "android": "android"}
MIMES = {
    "audio": {"audio/webm", "audio/ogg", "audio/wav"},
    "camera": {"image/jpeg", "image/png"},
    "android": {"application/xml"},
    "speech": {"text/plain"},
}


class MediaSessionRequest(StrictModel):
    device_id: str = Field(min_length=1, max_length=128)
    modalities: list[Literal["voice", "camera", "android"]] = Field(min_length=1, max_length=3)
    duration_seconds: int = Field(default=120, ge=5, le=600)
    max_bytes: int = Field(default=1_000_000, ge=1024, le=50_000_000)


class MediaChunk(StrictModel):
    request_key: str = Field(min_length=8, max_length=128)
    operation: Literal["audio", "speech", "camera", "android"]
    mime_type: str = Field(max_length=64)
    data_base64: str = Field(default="", max_length=86000)
    text: str = Field(default="", max_length=1000)


def android_text(data):
    """Extract visible accessibility labels only, with no ADB execution in Core."""
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise CoreError("INVALID_ANDROID_SNAPSHOT", 400)
    start, end = data.find(b"<?xml"), data.rfind(b"</hierarchy>")
    if start < 0:
        start = data.find(b"<hierarchy")
    if start < 0 or end < start:
        raise CoreError("INVALID_ANDROID_SNAPSHOT", 400)
    try:
        root = ET.fromstring(data[start : end + len(b"</hierarchy>")])
    except ET.ParseError as error:
        raise CoreError("INVALID_ANDROID_SNAPSHOT", 400) from error
    lines = []
    for node in root.iter("node"):
        if node.get("password") == "true":
            continue
        for key in ("text", "content-desc"):
            value = node.get(key, "").strip()
            if value and value not in lines:
                lines.append(value)
    text = "\n".join(lines)[:12000]
    if not text:
        raise CoreError("ANDROID_VISIBLE_TEXT_EMPTY", 422)
    return text


class MediaService:
    def __init__(self, store, settings, clock, observations, options=None, provider=None):
        self.store, self.settings, self.clock, self.observations = (
            store,
            settings,
            clock,
            observations,
        )
        self.options = options or settings.media
        self.provider = provider or HTTPMediaProvider(self.options)
        self.inflight = {}

    def _enabled(self):
        if not self.options.enabled:
            raise CoreError("MEDIA_DISABLED", 403)

    def _safe_text(self, text, session_token):
        secrets_to_check = [session_token] + [
            endpoint.key.get_secret_value()
            for endpoint in (self.options.stt, self.options.tts, self.options.vision)
        ]
        if redact(text, self.settings) != text or any(
            secret and secret in text for secret in secrets_to_check
        ):
            raise CoreError("MEDIA_CONTAINS_CREDENTIALS", 400)

    def start(self, request):
        self._enabled()
        modalities = sorted(set(request.modalities))
        if "android" in modalities and not re.fullmatch(
            r"adb:[A-Za-z0-9_.:-]{1,124}", request.device_id
        ):
            raise CoreError("ANDROID_DEVICE_ID_REQUIRED", 400)
        for modality in modalities:
            adapter = self.observations.policy(getattr(self.options, modality + "_adapter"))
            source = "voice" if modality == "voice" else modality
            if adapter.source != source or (source != "android" and not adapter.media_analysis):
                raise CoreError("MEDIA_ADAPTER_NOT_CONFIGURED", 503)
        session_id, token, now = uid("media"), secrets.token_urlsafe(32), self.clock()
        duration = min(request.duration_seconds, self.options.max_session_seconds)
        # ConsentService persists the explicit session grant after checking capability flags.
        consent = self.observations.consents.grant(
            ConsentRequest(
                capabilities=[CAPABILITIES[m] for m in modalities],
                device_id=request.device_id,
                session_id=session_id,
                duration_seconds=duration,
            )
        )
        try:
            with self.store.transaction():
                active = self.store.one(
                    "SELECT COUNT(*) AS n FROM media_sessions WHERE state = 'OPEN' AND expires_at "
                    "> %s",
                    (now,),
                )["n"]
                if active >= self.options.max_active_sessions:
                    raise CoreError("MEDIA_SESSION_LIMIT", 429)
                self.store.insert(
                    "media_sessions",
                    session_id=session_id,
                    device_id=request.device_id,
                    consent_id=consent["consent_id"],
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    modalities_json=encode(modalities),
                    state="OPEN",
                    captured_privacy_epoch=epoch(self.store),
                    expires_at=after(now, duration),
                    max_bytes=min(request.max_bytes, self.options.max_session_bytes),
                    created_at=now,
                )
        except BaseException:
            with self.store.transaction():
                self.store.execute(
                    "UPDATE consents SET revoked_at = %s WHERE consent_id = %s",
                    (self.clock(), consent["consent_id"]),
                )
            raise
        return {**self.status(session_id, token), "session_token": token}

    def _row(self, session_id, token):
        row = self.store.one(
            "SELECT media_sessions.session_id AS session_id, "
            "media_sessions.device_id AS device_id, media_sessions.consent_id AS consent_id, "
            "media_sessions.token_hash AS token_hash, media_sessions.modalities_json AS "
            "modalities_json, media_sessions.state AS state, media_sessions.captured_privacy_epoch "
            "AS captured_privacy_epoch, media_sessions.expires_at AS expires_at, "
            "media_sessions.bytes_reserved AS bytes_reserved, media_sessions.max_bytes AS "
            "max_bytes, media_sessions.close_reason AS close_reason, media_sessions.created_at AS "
            "created_at, media_sessions.closed_at AS closed_at FROM media_sessions AS "
            "media_sessions WHERE media_sessions.session_id = %s",
            (session_id,),
        )
        if not row or not hmac.compare_digest(
            row["token_hash"],
            hashlib.sha256(token.encode()).hexdigest(),
        ):
            raise CoreError("MEDIA_UNAUTHORIZED", 401)
        return row

    def _require(self, row, modality=None):
        self._enabled()
        if row["state"] != "OPEN" or row["expires_at"] <= self.clock():
            raise CoreError("MEDIA_SESSION_CLOSED", 410)
        if row["captured_privacy_epoch"] != epoch(self.store):
            raise CoreError("PRIVACY_CHANGED", 409)
        modalities = json.loads(row["modalities_json"])
        if modality and modality not in modalities:
            raise CoreError("MEDIA_MODALITY_NOT_GRANTED", 403)
        for name in modalities:
            self.observations.consents.require(
                row["consent_id"],
                CAPABILITIES[name],
                row["device_id"],
                row["session_id"],
            )

    def status(self, session_id, token):
        row = self._row(session_id, token)
        self._require(row)
        return {
            key: row[key]
            for key in (
                "session_id",
                "device_id",
                "consent_id",
                "expires_at",
                "bytes_reserved",
                "max_bytes",
            )
        } | {
            "modalities": json.loads(row["modalities_json"]),
            "max_chunk_bytes": self.options.max_chunk_bytes,
            "remaining_seconds": max(
                0,
                (
                    datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(self.clock())
                ).total_seconds(),
            ),
            "raw_media_retention": "discard",
        }

    def close(self, session_id, token=None, owner=False):
        row = (
            self.store.one(
                "SELECT media_sessions.session_id AS session_id, "
                "media_sessions.device_id AS device_id, media_sessions.consent_id AS consent_id, "
                "media_sessions.token_hash AS token_hash, media_sessions.modalities_json AS "
                "modalities_json, media_sessions.state AS state, "
                "media_sessions.captured_privacy_epoch AS captured_privacy_epoch, "
                "media_sessions.expires_at AS expires_at, media_sessions.bytes_reserved AS "
                "bytes_reserved, media_sessions.max_bytes AS max_bytes, "
                "media_sessions.close_reason AS close_reason, media_sessions.created_at AS "
                "created_at, media_sessions.closed_at AS closed_at FROM media_sessions AS "
                "media_sessions WHERE media_sessions.session_id = %s",
                (session_id,),
            )
            if owner
            else self._row(session_id, token or "")
        )
        if not row:
            raise CoreError("MEDIA_SESSION_NOT_FOUND", 404)
        with self.store.transaction():
            self.store.execute(
                "UPDATE media_sessions SET state = 'CLOSED', closed_at = %s, close_reason = "
                "'OWNER_STOP' WHERE session_id = %s",
                (self.clock(), session_id),
            )
        # Closing one session does not revoke all other sessions via the global privacy epoch.
        with self.store.transaction():
            self.store.execute(
                "UPDATE consents SET revoked_at = %s WHERE consent_id = %s",
                (self.clock(), row["consent_id"]),
            )
        for task in tuple(self.inflight.get(session_id, ())):
            task.cancel()
        return {"session_id": session_id, "state": "CLOSED"}

    def _decode(self, chunk):
        if chunk.mime_type not in MIMES[chunk.operation]:
            raise CoreError("UNSUPPORTED_MEDIA_TYPE", 415)
        if chunk.operation == "speech":
            if not chunk.text.strip() or chunk.data_base64:
                raise CoreError("INVALID_MEDIA_BODY", 400)
            data = chunk.text.encode()
        else:
            if chunk.text:
                raise CoreError("INVALID_MEDIA_BODY", 400)
            try:
                data = base64.b64decode(chunk.data_base64, validate=True)
            except (ValueError, binascii.Error) as error:
                raise CoreError("INVALID_MEDIA_BODY", 400) from error
        if not data or len(data) > self.options.max_chunk_bytes:
            raise CoreError("MEDIA_CHUNK_TOO_LARGE", 413)
        if chunk.operation == "camera" and not (
            (chunk.mime_type == "image/jpeg" and data.startswith(b"\xff\xd8\xff"))
            or (chunk.mime_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
        ):
            raise CoreError("INVALID_MEDIA_BODY", 400)
        if chunk.operation == "audio" and not (
            (chunk.mime_type == "audio/webm" and data.startswith(b"\x1aE\xdf\xa3"))
            or (chunk.mime_type == "audio/ogg" and data.startswith(b"OggS"))
            or (
                chunk.mime_type == "audio/wav"
                and data.startswith(b"RIFF")
                and data[8:12] == b"WAVE"
            )
        ):
            raise CoreError("INVALID_MEDIA_BODY", 400)
        return data

    def _reserve(self, row, chunk, data, endpoint):
        if self.settings.mode == "live" or (
            isinstance(self.provider, HTTPMediaProvider)
            and not isinstance(self.provider.transport, httpx.MockTransport)
        ):
            raise CoreError("PAID_MEDIA_INPUT_CONTRACT_UNPROVEN", 503)
        now, store = self.clock(), self.store
        fingerprint = hashlib.sha256(
            chunk.operation.encode() + b"\0" + chunk.mime_type.encode() + b"\0" + data
        ).hexdigest()
        local = datetime.fromisoformat(now).astimezone(ZoneInfo(self.settings.timezone))
        day = (
            local.replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(UTC)
            .isoformat(timespec="microseconds")
        )
        reserve_bytes = len(data) + (
            self.options.max_output_bytes if chunk.operation == "speech" else 0
        )
        with store.transaction():
            row = store.one(
                "SELECT media_sessions.session_id AS session_id, "
                "media_sessions.device_id AS device_id, media_sessions.consent_id AS consent_id, "
                "media_sessions.token_hash AS token_hash, media_sessions.modalities_json AS "
                "modalities_json, media_sessions.state AS state, "
                "media_sessions.captured_privacy_epoch AS captured_privacy_epoch, "
                "media_sessions.expires_at AS expires_at, media_sessions.bytes_reserved AS "
                "bytes_reserved, media_sessions.max_bytes AS max_bytes, "
                "media_sessions.close_reason AS close_reason, media_sessions.created_at AS "
                "created_at, media_sessions.closed_at AS closed_at FROM media_sessions AS "
                "media_sessions WHERE media_sessions.session_id = %s",
                (row["session_id"],),
            )
            self._require(row, OPERATIONS[chunk.operation])
            previous = store.one(
                "SELECT media_requests.request_id AS request_id, media_requests.session_id AS "
                "session_id, media_requests.request_key AS request_key, media_requests.fingerprint "
                "AS fingerprint, media_requests.operation AS operation, media_requests.state AS "
                "state, media_requests.reserved_bytes AS reserved_bytes, "
                "media_requests.usage_request_id AS usage_request_id, media_requests.event_id AS "
                "event_id, media_requests.created_at AS created_at FROM media_requests AS "
                "media_requests WHERE media_requests.session_id = %s AND "
                "media_requests.request_key = %s",
                (row["session_id"], chunk.request_key),
            )
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise CoreError("IDEMPOTENCY_CONFLICT", 409)
                # Raw audio is never cached for replay. Ambiguous calls cannot be repeated for free.
                raise CoreError("MEDIA_REQUEST_ALREADY_" + previous["state"], 409)
            daily_bytes = store.one(
                (
                    "SELECT COALESCE(SUM(reserved_bytes),0) AS n FROM "
                    "media_requests WHERE created_at>=%s"
                ),
                (day,),
            )["n"]
            if (
                row["bytes_reserved"] + reserve_bytes > row["max_bytes"]
                or daily_bytes + reserve_bytes > self.options.max_daily_bytes
            ):
                raise CoreError("MEDIA_TRAFFIC_BUDGET_EXHAUSTED", 429)
            usage_id = None
            if endpoint:
                if not endpoint.url or not endpoint.model or endpoint.max_request_micro_usd == 0:
                    raise CoreError("MEDIA_PROVIDER_NOT_CONFIGURED", 503)
                price = endpoint.max_request_micro_usd
                usage_id = uid("media_cost")
                cloud_budget.reserve(
                    store,
                    self.settings,
                    reference_type="model",
                    reference_id=usage_id,
                    estimated_micro_usd=price,
                    now=now,
                    task_kind="asr" if chunk.operation == "audio" else "model",
                    task_id="media-session:" + row["session_id"]
                    if chunk.operation == "audio"
                    else "media-request:" + row["session_id"] + ":" + chunk.request_key,
                    pricing={
                        "pricing_known": True,
                        "provider": "media-http",
                        "model": endpoint.model,
                        "request_estimate_micro_usd": price,
                    },
                )
                store.insert(
                    "model_usage",
                    usage_id=uid("usage"),
                    request_id=usage_id,
                    scenario="media",
                    provider="media-http",
                    model=endpoint.model,
                    billed_micro_usd=price,
                    state="RESERVED",
                    pricing_known=1,
                    is_estimate=1,
                    occurred_at=now,
                )
            request_id = uid("media_req")
            store.insert(
                "media_requests",
                request_id=request_id,
                session_id=row["session_id"],
                request_key=chunk.request_key,
                fingerprint=fingerprint,
                operation=chunk.operation,
                state="RESERVED",
                reserved_bytes=reserve_bytes,
                usage_request_id=usage_id,
                created_at=now,
            )
            store.execute(
                "UPDATE media_sessions SET bytes_reserved = bytes_reserved + %s WHERE session_id = "
                "%s",
                (reserve_bytes, row["session_id"]),
            )
        return request_id, usage_id

    async def process(self, session_id, token, chunk):
        row = self._row(session_id, token)
        self._require(row, OPERATIONS[chunk.operation])
        data = self._decode(chunk)
        endpoint = {
            "audio": self.options.stt,
            "speech": self.options.tts,
            "camera": self.options.vision,
            "android": None,
        }[chunk.operation]
        if chunk.operation == "speech":
            self._safe_text(chunk.text, token)
        # Validate untrusted XML locally before reserving a request or extracting text.
        extracted = android_text(data) if chunk.operation == "android" else None
        request_id, usage_id = self._reserve(row, chunk, data, endpoint)
        task = asyncio.current_task()
        self.inflight.setdefault(session_id, set()).add(task)
        try:
            if usage_id:
                with self.store.transaction():
                    cloud_budget.mark_dispatched(self.store, "model", usage_id, self.clock())
            async with asyncio.timeout(self.options.provider_timeout_seconds):
                result = (
                    MediaResult(text=extracted)
                    if extracted is not None
                    else await self.provider.process(
                        chunk.operation, data, chunk.mime_type, endpoint
                    )
                )
            # The final authorization check, evidence and accounting form one commit.
            # Erasure on a second connection can run before or after it, never in between.
            with self.store.transaction():
                fresh = self.store.one(
                    "SELECT media_sessions.session_id AS session_id, media_sessions.device_id AS "
                    "device_id, media_sessions.consent_id AS consent_id, media_sessions.token_hash "
                    "AS token_hash, media_sessions.modalities_json AS modalities_json, "
                    "media_sessions.state AS state, media_sessions.captured_privacy_epoch AS "
                    "captured_privacy_epoch, media_sessions.expires_at AS expires_at, "
                    "media_sessions.bytes_reserved AS bytes_reserved, media_sessions.max_bytes AS "
                    "max_bytes, media_sessions.close_reason AS close_reason, "
                    "media_sessions.created_at AS created_at, media_sessions.closed_at AS "
                    "closed_at FROM media_sessions AS media_sessions WHERE "
                    "media_sessions.session_id = %s",
                    (session_id,),
                )
                self._require(fresh, OPERATIONS[chunk.operation])
                if chunk.operation == "speech":
                    if (
                        not result.audio
                        or len(result.audio) > self.options.max_output_bytes
                        or result.mime_type != "audio/mpeg"
                        or not (
                            result.audio.startswith(b"ID3")
                            or (
                                len(result.audio) > 1
                                and result.audio[0] == 255
                                and result.audio[1] & 224 == 224
                            )
                        )
                    ):
                        raise CoreError("INVALID_MEDIA_PROVIDER_RESPONSE", 502)
                    response = {
                        "request_id": request_id,
                        "audio_base64": base64.b64encode(result.audio).decode(),
                        "mime_type": result.mime_type,
                        "raw_media_retention": "discard",
                    }
                    event_id = None
                else:
                    self._safe_text(result.text, token)
                    source = OPERATIONS[chunk.operation]
                    kind = {
                        "audio": "audio_transcript",
                        "camera": "image_analysis",
                        "android": "text",
                    }[chunk.operation]
                    event_id = uid("obs_media")
                    item = ObservationEvent(
                        schema_version=2,
                        event_id=event_id,
                        event_type="content.observed",
                        source=source,
                        source_kind=kind,
                        provider_event_id=request_id,
                        occurred_at=self.clock(),
                        trace_id=request_id,
                        result_status="FETCHED",
                        privacy_epoch=row["captured_privacy_epoch"],
                        source_sha256=hashlib.sha256(result.text.encode()).hexdigest(),
                        device_id=row["device_id"],
                        session_id=session_id,
                        consent_id=row["consent_id"],
                        payload={
                            "text": result.text,
                            "media_sha256": hashlib.sha256(data).hexdigest(),
                            "processor_id": endpoint.model
                            if endpoint
                            else "android-uiautomator-text-v1",
                        },
                    )
                    self.observations.append(getattr(self.options, source + "_adapter"), item)
                    response = {
                        "request_id": request_id,
                        "event_id": event_id,
                        "text": result.text,
                        "raw_media_retention": "discard",
                    }
                self.store.execute(
                    "UPDATE media_requests SET state = 'SUCCEEDED', event_id = %s WHERE request_id "
                    "= %s",
                    (event_id, request_id),
                )
                if usage_id:
                    self.store.execute(
                        "UPDATE model_usage SET state = 'SETTLED' WHERE request_id = %s",
                        (usage_id,),
                    )
                    cloud_budget.sync_reference(self.store, "model", usage_id)
            return response
        except BaseException as error:
            with self.store.transaction():
                self.store.execute(
                    "UPDATE media_requests SET state = 'UNKNOWN' WHERE request_id = %s",
                    (request_id,),
                )
                if usage_id:
                    self.store.execute(
                        "UPDATE model_usage SET state = 'UNKNOWN' WHERE request_id = %s",
                        (usage_id,),
                    )
                    cloud_budget.sync_reference(self.store, "model", usage_id)
            if isinstance(error, TimeoutError):
                raise CoreError("MEDIA_PROVIDER_UNCERTAIN", 502) from error
            raise
        finally:
            self.inflight[session_id].discard(task)
            if not self.inflight[session_id]:
                self.inflight.pop(session_id, None)


def erase_media_links(store, event_ids):
    """Drop derived media digests on erasure; accounting is retained without source content."""
    if not store.one(
        "SELECT 1 FROM pg_catalog.pg_class WHERE "
        "relnamespace=current_schema()::regnamespace AND relname='media_requests' AND relkind IN "
        "('r','p')"
    ):
        return
    for event_id in event_ids:
        store.execute(
            "UPDATE media_requests SET event_id = NULL, fingerprint = '' WHERE event_id = %s",
            (event_id,),
        )
