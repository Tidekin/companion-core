"""Storage-free voice execution, result, and delivery contracts."""

import base64
import binascii
import hashlib
import json
import wave
from datetime import timedelta
from io import BytesIO
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from companion_delivery_protocol import singing
from companion_delivery_protocol.authority import validate_authority_ref

PROTOCOL_VERSION = 2
LEASE_DOMAIN = b"companion-core/voice-node-lease/v2\x00"
ROUTE_PREFIX = "/internal/v2/voice-node"
MAX_INPUT_BYTES = 8_000_000
MAX_AUDIO_BYTES = 4_000_000
MAX_AUDIO_BASE64 = 4 * ((MAX_AUDIO_BYTES + 2) // 3)
MAX_RESULT_BODY_BYTES = 5_500_000

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
JobId = Annotated[str, Field(pattern=r"^voice_[a-f0-9]{32}$")]
NodeId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
Generation = Annotated[int, Field(ge=1, le=2**63 - 1, strict=True)]
LeaseToken = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{32,256}$", repr=False)]
ErrorCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
ResourceState = Literal["READY", "RUNNING", "QUARANTINED"]
Operation = Literal["tts", "asr", "sing"]


class VoiceProtocolError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class VoiceDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    @model_validator(mode="before")
    @classmethod
    def strict_literal_integers(cls, value):
        if isinstance(value, dict) and any(
            name in value and type(value[name]) is not int
            for name in ("schema_version", "channels", "sample_width")
        ):
            raise ValueError("Voice integer literals must be integers")
        return value


def canonical_bytes(value: BaseModel | dict) -> bytes:
    """Fixed v1 JSON encoding, also used for signed leases and result fingerprints."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value: BaseModel | dict) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


class VoiceClaimRequest(VoiceDTO):
    schema_version: Literal[2] = PROTOCOL_VERSION
    node_id: NodeId
    incarnation: str = Field(pattern=r"^[a-f0-9]{32}$")
    sequence: Generation
    manifest_sha256: Digest
    resource_state: Literal["READY", "QUARANTINED"]

    @property
    def request_id(self):
        return f"voice-claim:{self.incarnation}:{self.sequence}"


class VoiceSession(VoiceDTO):
    incarnation: str = Field(pattern=r"^[a-f0-9]{32}$")
    last_sequence: int = Field(ge=0, le=2**63-1, strict=True)
    replay_available: bool = Field(strict=True)
    recovery_required: bool = Field(strict=True)


class VoiceStopRef(VoiceDTO):
    job_id: JobId
    generation: Generation
    stop_token: LeaseToken

    @classmethod
    def from_lease(cls, lease):
        return cls(job_id=lease.job_id, generation=lease.generation, stop_token=lease.stop_token)


class VoiceCleanup(VoiceDTO):
    cache_removed: bool = Field(strict=True)
    journal_removed: bool = Field(strict=True)
    temp_removed: bool = Field(strict=True)

    def complete(self):
        return self.cache_removed and self.journal_removed and self.temp_removed


class VoiceStopRequest(VoiceDTO):
    ref: VoiceStopRef
    resource_state: ResourceState
    outcome: Literal["COMMITTED", "CANCELLED", "FAILED", "UNKNOWN", "RESULT_LOST"]
    cleanup: VoiceCleanup
    cleanup_epoch: int = Field(ge=0, strict=True)


class VoiceStopResponse(VoiceDTO):
    custody_closed: bool = Field(strict=True)
    cleanup_confirmed: bool = Field(strict=True)
    cleanup_epoch: int = Field(ge=0, strict=True)


class VoiceRecovery(VoiceDTO):
    incarnation: str = Field(pattern=r"^[a-f0-9]{32}$")
    operator: str = Field(min_length=1, max_length=128)
    service_instance: str = Field(min_length=1, max_length=128)
    confirmation: Literal["inflight-execution-stopped"]


class VoiceLease(VoiceDTO):
    schema_version: Literal[2] = PROTOCOL_VERSION
    incarnation: str = Field(pattern=r"^[a-f0-9]{32}$")
    stop_token: LeaseToken
    claim_sequence: Generation
    privacy_epoch: int = Field(ge=0, strict=True)
    job_id: JobId
    generation: Generation
    node_id: NodeId
    operation: Operation
    lease_token: LeaseToken
    input_sha256: Digest
    input_bytes: int = Field(gt=0, le=MAX_INPUT_BYTES, strict=True)
    input_mime: Literal["text/plain", "audio/wav", "application/json"]
    source_partial: bool = Field(default=False, strict=True)
    manifest_sha256: Digest
    issued_at: AwareDatetime
    server_time: AwareDatetime
    lease_expires_at: AwareDatetime
    job_deadline_at: AwareDatetime

    @model_validator(mode="after")
    def valid_scope(self):
        times = (self.issued_at, self.server_time, self.lease_expires_at, self.job_deadline_at)
        if any(value.utcoffset() != timedelta(0) for value in times):
            raise ValueError("Voice lease times must be UTC")
        if not self.issued_at <= self.server_time < self.lease_expires_at <= self.job_deadline_at:
            raise ValueError("Voice lease time ordering is invalid")
        if self.lease_expires_at - self.server_time > timedelta(
            seconds=120
        ) or self.job_deadline_at - self.issued_at > timedelta(seconds=600):
            raise ValueError("Voice lease exceeds the protocol duration limit")
        if self.operation == "tts":
            if self.input_mime != "text/plain" or self.source_partial or self.input_bytes > 16000:
                raise ValueError("TTS lease input must be bounded UTF-8 text")
        elif self.operation == "sing":
            if (self.input_mime != "application/json" or self.source_partial
                or self.input_bytes > 16384):
                raise ValueError("Singing input must be a bounded source-bound recipe")
        elif self.input_mime != "audio/wav":
            raise ValueError("ASR lease input must be WAV")
        return self


class SignedVoiceLease(VoiceLease):
    signature: str = Field(pattern=r"^[a-f0-9]{128}$", repr=False)


def sign_voice_lease(lease: VoiceLease, private_key: Ed25519PrivateKey) -> SignedVoiceLease:
    unsigned = lease.model_dump(mode="json", exclude={"signature"})
    signature = private_key.sign(LEASE_DOMAIN + canonical_bytes(unsigned)).hex()
    return SignedVoiceLease(**unsigned, signature=signature)


def verify_voice_lease(
    lease: SignedVoiceLease,
    public_key: bytes,
    *,
    node_id: str,
    manifest_sha256: str,
) -> None:
    if lease.node_id != node_id or lease.manifest_sha256 != manifest_sha256:
        raise VoiceProtocolError("VOICE_LEASE_SCOPE_MISMATCH")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            bytes.fromhex(lease.signature),
            LEASE_DOMAIN + canonical_bytes(lease.model_dump(mode="json", exclude={"signature"})),
        )
    except (InvalidSignature, ValueError):
        raise VoiceProtocolError("VOICE_LEASE_SIGNATURE_INVALID") from None


class VoiceClaimResponse(VoiceDTO):
    lease: SignedVoiceLease | None = None
    retry_after_seconds: int = Field(default=5, ge=1, le=60, strict=True)


class VoiceLeaseRef(VoiceDTO):
    job_id: JobId
    generation: Generation
    lease_token: LeaseToken
    input_sha256: Digest
    manifest_sha256: Digest

    @classmethod
    def from_lease(cls, lease: VoiceLease):
        return cls(**{key: getattr(lease, key) for key in cls.model_fields})


class VoiceControlRequest(VoiceDTO):
    ref: VoiceLeaseRef
    phase: Literal[
        "CLAIMED",
        "RUNNING",
        "RESULT_READY",
        "FAILED",
        "CANCELLED",
        "UNKNOWN",
        "RESULT_LOST",
    ]
    resource_state: ResourceState
    result_sha256: Digest | None = None
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def result_identity(self):
        if self.phase == "RESULT_READY" and self.result_sha256 is None:
            raise ValueError("A ready result requires its digest")
        return self


class VoiceControlResponse(VoiceDTO):
    action: Literal["CONTINUE", "CANCEL", "EXPIRED", "SUPERSEDED", "COMMITTED"]
    renewed_lease: SignedVoiceLease | None = None
    committed_result_sha256: Digest | None = None

    @model_validator(mode="after")
    def action_payload(self):
        if (self.action == "CONTINUE") != (self.renewed_lease is not None):
            raise ValueError("Only CONTINUE carries a renewed lease")
        if (self.action == "COMMITTED") != (self.committed_result_sha256 is not None):
            raise ValueError("Only COMMITTED carries a result receipt digest")
        return self


class VoiceAudioResult(VoiceDTO):
    kind: Literal["tts"] = "tts"
    audio_base64: str = Field(min_length=60, max_length=MAX_AUDIO_BASE64, repr=False)
    audio_sha256: Digest
    sample_rate: int = Field(ge=8000, le=96000, strict=True)
    channels: Literal[1, 2]
    sample_width: Literal[2] = 2
    frames: int = Field(gt=0, le=5_760_000, strict=True)
    duration_seconds: float = Field(gt=0, le=60, strict=True, allow_inf_nan=False)
    mime_type: Literal["audio/wav"] = "audio/wav"

    def audio_bytes(self) -> bytes:
        try:
            data = base64.b64decode(self.audio_base64, validate=True)
        except (ValueError, binascii.Error):
            raise VoiceProtocolError("VOICE_RESULT_AUDIO_INVALID") from None
        if not 44 <= len(data) <= MAX_AUDIO_BYTES or hashlib.sha256(data).hexdigest() != (
            self.audio_sha256
        ):
            raise VoiceProtocolError("VOICE_RESULT_AUDIO_INVALID")
        return data

    @model_validator(mode="after")
    def audio_identity(self):
        data = self.audio_bytes()
        if (
            data[:4] != b"RIFF"
            or data[8:12] != b"WAVE"
            or int.from_bytes(data[4:8], "little") != len(data) - 8
            or base64.b64encode(data).decode() != self.audio_base64
        ):
            raise ValueError("Voice audio must be a complete canonical WAV")
        offset, fmt, data_size = 12, None, None
        while offset < len(data):
            if offset + 8 > len(data):
                raise ValueError("Voice WAV chunk is incomplete")
            kind = data[offset : offset + 4]
            length = int.from_bytes(data[offset + 4 : offset + 8], "little")
            offset += 8
            if offset + length + length % 2 > len(data):
                raise ValueError("Voice WAV chunk is incomplete")
            if kind == b"fmt ":
                if fmt is not None or length < 16:
                    raise ValueError("Voice WAV format chunk is invalid")
                fmt = data[offset : offset + 16]
            elif kind == b"data":
                if fmt is None or data_size is not None:
                    raise ValueError("Voice WAV data chunk is invalid")
                data_size = length
            offset += length + length % 2
        if (
            fmt is None
            or int.from_bytes(fmt[:2], "little") != 1
            or int.from_bytes(fmt[14:16], "little") != 16
            or int.from_bytes(fmt[12:14], "little") != self.channels * 2
            or int.from_bytes(fmt[8:12], "little") != self.sample_rate * self.channels * 2
            or data_size != self.frames * self.channels * 2
        ):
            raise ValueError("Voice WAV metadata differs from PCM16 content")
        try:
            with wave.open(BytesIO(data), "rb") as audio:
                if (
                    audio.getframerate() != self.sample_rate
                    or audio.getnchannels() != self.channels
                    or audio.getsampwidth() != 2
                    or audio.getnframes() != self.frames
                    or len(audio.readframes(self.frames + 1)) != data_size
                ):
                    raise ValueError("Voice WAV metadata differs from content")
        except (wave.Error, EOFError):
            raise ValueError("Voice WAV content is invalid") from None
        if abs(self.frames / self.sample_rate - self.duration_seconds) > 1e-9:
            raise ValueError("Voice duration does not match the frame count")
        return self


class SingingAudio(VoiceAudioResult):
    kind: Literal["sing"] = "sing"


class SingingResult(VoiceDTO):
    kind: Literal["sing"] = "sing"
    manifest: dict
    audio: tuple[SingingAudio, ...] = Field(min_length=1, max_length=singing.MAX_SEGMENTS,
                                          repr=False)

    @model_validator(mode="after")
    def complete_source_partition(self):
        singing.validate_manifest(self.manifest)
        if len(self.audio) != len(self.manifest["segments"]):
            raise ValueError("singing audio count differs")
        pcm = hashlib.sha256()
        for index, audio in enumerate(self.audio):
            data = audio.audio_bytes()
            singing.validate_part(self.manifest, index, {
                "audio_bytes": len(data), **audio.model_dump(exclude={"audio_base64"})})
            with wave.open(BytesIO(data), "rb") as recording:
                pcm.update(recording.readframes(audio.frames))
        if pcm.hexdigest() != self.manifest["pcm_sha256"]:
            raise ValueError("singing PCM coverage digest differs")
        return self


def result_digest(payload):
    # Per-part byte hashes commit to the full audio. The compact parent manifest
    # lets the independent Bridge verify each part without receiving every WAV.
    return digest(payload.manifest) if isinstance(payload, SingingResult) else digest(payload)


class _VoiceDeliveryMaterial(VoiceDTO):
    retirement_token: LeaseToken
    job_id: JobId
    generation: Generation
    message_id: str = Field(pattern=r"^msg_[a-f0-9]{32}$")
    delivery_key: str = Field(min_length=1, max_length=512)
    event_id: str = Field(min_length=1, max_length=128)
    source_sha256: Digest
    identity_sha256: Digest
    privacy_epoch: int = Field(ge=0, strict=True)
    reply_revision: int = Field(ge=0, strict=True)
    node_id: NodeId
    manifest_sha256: Digest
    node_config_sha256: Digest
    voice_policy_sha256: Digest
    content_sha256: Digest
    reply_sha256: Digest
    result_sha256: Digest
    audio_sha256: Digest
    audio_bytes: int = Field(ge=44, le=4_000_000, strict=True)
    expires_at: AwareDatetime


class LegacyVoiceDeliveryBinding(_VoiceDeliveryMaterial):
    """Read-only schema used to retire historical v2 delivery custody."""

    schema_version: Literal[2] = 2
    window_sha256: Digest


class VoiceDeliveryBinding(_VoiceDeliveryMaterial):
    schema_version: Literal[3] = 3
    authority: dict
    singing_segment_index: int | None = Field(default=None, ge=0, lt=singing.MAX_SEGMENTS,
        strict=True, exclude_if=lambda value: value is None)

    @field_validator("authority")
    @classmethod
    def original_authority(cls, value):
        return validate_authority_ref(value)


class VoiceRetiredBinding(VoiceDTO):
    kind: Literal["capability", "legacy_signature"]
    verifier: Digest
    job_id: JobId
    generation: Generation
    message_id: str = Field(pattern=r"^msg_[a-f0-9]{32}$")
    event_id: str = Field(min_length=1, max_length=128)
    delivery_key: str = Field(min_length=1, max_length=512)
    window_sha256: Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    authority: dict | None = Field(default=None, exclude_if=lambda value: value is None)
    voice_policy_sha256: Digest
    reply_revision: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def original_scope(self):
        if (self.window_sha256 is None) == (self.authority is None):
            raise ValueError("retirement requires exactly one original authority")
        if self.authority is not None:
            validate_authority_ref(self.authority)
        return self


class VoiceDeliveryEnvelope(VoiceDTO):
    binding: VoiceDeliveryBinding
    binding_sha256: Digest
    audio: Annotated[VoiceAudioResult | SingingAudio, Field(discriminator="kind")]
    singing_manifest: dict | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def bound_audio(self):
        if self.audio.kind == "sing":
            singing.validate_part(self.singing_manifest, self.binding.singing_segment_index, {
                "audio_bytes": len(self.audio.audio_bytes()),
                **self.audio.model_dump(exclude={"audio_base64"})})
            expected = digest(self.singing_manifest)
            if self.singing_manifest["node_manifest_sha256"] != self.binding.manifest_sha256:
                raise ValueError("Singing processor manifest changed")
        else:
            if self.singing_manifest is not None or self.binding.singing_segment_index is not None:
                raise ValueError("TTS must not carry singing material")
            expected = digest(self.audio)
        if (
            self.binding_sha256 != digest(self.binding)
            or self.binding.result_sha256 != expected
            or self.binding.audio_sha256 != self.audio.audio_sha256
            or self.binding.audio_bytes != len(self.audio.audio_bytes())
        ):
            raise ValueError("Voice delivery audio differs from its frozen binding")
        return self


class VoiceTranscriptSegment(VoiceDTO):
    start_seconds: float = Field(ge=0, le=120, strict=True, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, le=120, strict=True, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=12000, repr=False)


class VoiceTranscriptResult(VoiceDTO):
    kind: Literal["asr"] = "asr"
    text: str = Field(max_length=12000, repr=False)
    segments: tuple[VoiceTranscriptSegment, ...] = Field(max_length=128, repr=False)
    language: str = Field(pattern=r"^[a-z]{2,3}$")
    language_probability: float = Field(ge=0, le=1, strict=True, allow_inf_nan=False)
    duration_seconds: float = Field(gt=0, le=120, strict=True, allow_inf_nan=False)
    partial: bool = Field(strict=True)

    @model_validator(mode="after")
    def transcript_evidence(self):
        last_end, total = 0.0, 0
        for segment in self.segments:
            if (
                not (
                    last_end <= segment.start_seconds < segment.end_seconds <= self.duration_seconds
                )
                or not segment.text.strip()
            ):
                raise ValueError("Voice transcript segment boundaries are invalid")
            last_end = segment.end_seconds
            total += len(segment.text)
        if total > 12000 or self.text != "".join(s.text for s in self.segments).strip():
            raise ValueError("Voice transcript text differs from its segments")
        return self


VoicePayload = Annotated[
    VoiceAudioResult | VoiceTranscriptResult | SingingResult, Field(discriminator="kind")]


class VoiceResult(VoiceDTO):
    ref: VoiceLeaseRef
    operation: Operation
    result_sha256: Digest
    payload: VoicePayload

    @model_validator(mode="after")
    def result_identity(self):
        if self.operation != self.payload.kind or self.result_sha256 != result_digest(self.payload):
            raise ValueError("Voice result identity is invalid")
        return self


def make_voice_result(ref: VoiceLeaseRef, payload: VoicePayload) -> VoiceResult:
    result = VoiceResult(
        ref=ref,
        operation=payload.kind,
        result_sha256=result_digest(payload),
        payload=payload,
    )
    limit = (singing.MAX_RESULT_BODY_BYTES if payload.kind == "sing" else MAX_RESULT_BODY_BYTES)
    if len(canonical_bytes(result)) > limit:
        raise VoiceProtocolError("VOICE_RESULT_TOO_LARGE")
    return result


class VoiceResultReceipt(VoiceDTO):
    job_id: JobId
    generation: Generation
    result_sha256: Digest
    status: Literal["COMMITTED"] = "COMMITTED"
