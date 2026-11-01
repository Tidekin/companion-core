"""Outbound-only voice node: fixed Core origin, signed leases, one supervised GPU slot."""

import argparse
import asyncio
import base64
import hashlib
import importlib.metadata
import ipaddress
import json
import math
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

from companion.voice_protocol import (
    MAX_AUDIO_BYTES,
    MAX_INPUT_BYTES,
    MAX_RESULT_BODY_BYTES,
    ROUTE_PREFIX,
    SignedVoiceLease,
    VoiceClaimRequest,
    VoiceClaimResponse,
    VoiceControlRequest,
    VoiceControlResponse,
    VoiceLeaseRef,
    VoicePayload,
    VoiceProtocolError,
    VoiceRecovery,
    VoiceResultReceipt,
    VoiceSession,
    VoiceStopRequest,
    VoiceStopResponse,
    canonical_bytes,
    digest,
    make_voice_result,
    verify_voice_lease,
)
from companion_voice_node.providers import FasterWhisperConfig, OpenAISpeechConfig, validate_wav
from companion_voice_node.resources import file_digest as _file_digest
from companion_voice_node.state import NodeState
from companion_voice_node.supervisor import (
    TTS_NOT_STARTED_CODES,
    ProcessSupervisor,
    process_has_exited,
)


class VoiceNodeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    node_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    core_origin: str
    core_connect_address: str | None = None
    credential: SecretStr = Field(repr=False, exclude=True)
    core_public_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    workspace: Path = Field(repr=False)
    state_directory: Path = Field(default=Path(".local/voice-node"), repr=False)
    tts: OpenAISpeechConfig | None = Field(default=None, repr=False)
    asr: FasterWhisperConfig | None = Field(default=None, repr=False)
    tts_service_config: Path | None = Field(default=None, repr=False)
    tts_server_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{40,64}$")
    heartbeat_seconds: float = Field(default=3, ge=0.05, le=30, allow_inf_nan=False)
    request_timeout_seconds: float = Field(default=10, gt=0, le=30, allow_inf_nan=False)
    execution_timeout_seconds: float = Field(default=90, gt=0, le=600, allow_inf_nan=False)
    startup_timeout_seconds: float = Field(default=10, gt=0, le=30, allow_inf_nan=False)
    result_cache_ttl_seconds: int = Field(default=120, ge=1, le=300, strict=True)

    @field_validator("core_origin")
    @classmethod
    def https_origin(cls, value):
        try:
            parts = urlsplit(value)
            valid = (
                parts.scheme == "https"
                and bool(parts.hostname)
                and parts.port != 0
                and parts.username is None
                and parts.password is None
                and parts.path in {"", "/"}
                and not parts.query
                and not parts.fragment
            )
        except ValueError:
            valid = False
        if not valid or "\\" in value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
            raise ValueError("core_origin must be a fixed HTTPS origin")
        return value.rstrip("/")

    @field_validator("credential")
    @classmethod
    def private_credential(cls, value):
        raw = value.get_secret_value()
        if not 32 <= len(raw) <= 256 or any(ord(char) <= 32 or ord(char) >= 127 for char in raw):
            raise ValueError("node credential must be a private printable token")
        return value

    @field_validator("core_connect_address")
    @classmethod
    def unicast_address(cls, value):
        if value is None:
            return None
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise ValueError("Core connect address must be a literal IP address") from None
        if address.is_unspecified or address.is_multicast:
            raise ValueError("Core connect address must be unicast")
        return str(address)

    @model_validator(mode="after")
    def local_configuration(self):
        if not self.workspace.is_absolute() or not self.workspace.is_dir():
            raise ValueError("workspace must be an existing absolute directory")
        root = self.workspace.resolve()
        state = self.state_directory
        if not state.is_absolute():
            state = root / state
        state = state.resolve()
        if state == root or not state.is_relative_to(root):
            raise ValueError("state_directory must remain inside the workspace")
        object.__setattr__(self, "workspace", root)
        object.__setattr__(self, "state_directory", state)
        if self.tts is None and self.asr is None:
            raise ValueError("at least one local provider must be explicitly configured")
        if self.tts is not None:
            if self.tts.api_key is not None and (
                self.tts.api_key.get_secret_value() == self.credential.get_secret_value()
            ):
                raise ValueError("TTS proxy credentials must be independent of the Core credential")
            if (
                self.tts_service_config is None
                or not self.tts_service_config.is_absolute()
                or self.tts_server_revision is None
                or self.tts.max_concurrency != 1
                or self.tts.output.max_bytes > MAX_AUDIO_BYTES
                or self.tts.output.max_duration_seconds > 60
            ):
                raise ValueError("TTS requires pinned local weights and bounded single-slot output")
        elif any(
            value is not None
            for value in (
                self.tts_service_config,
                self.tts_server_revision,
            )
        ):
            raise ValueError("TTS resources require a TTS provider configuration")
        if self.asr is not None and (
            self.asr.max_concurrency != 1
            or self.asr.input.max_bytes > MAX_INPUT_BYTES
            or self.asr.input.max_duration_seconds > 120
            or self.asr.max_segments > 128
            or self.asr.max_text_chars > 12000
        ):
            raise ValueError("ASR must fit the bounded single-slot protocol")
        return self


def local_manifest(config: VoiceNodeConfig) -> dict:
    """Hash actual operator-controlled resources, never cloud-supplied paths.

    The configured resource files describe the intended service instance;
    this is not remote attestation that an unmanaged HTTP server loaded those files.
    """
    manifest = {
        "schema_version": 1,
        "providers": {},
        "execution_timeout_seconds": config.execution_timeout_seconds,
    }
    if config.tts is not None:
        from companion_voice_node.speech_service import load_service_config, service_manifest

        options = config.tts.model_dump(
            mode="json", exclude={"api_key"}
        )
        service = load_service_config(config.tts_service_config)
        if (service.model != config.tts.model or service.voice != config.tts.voice
                or service.base.max_concurrency != 1):
            raise VoiceProtocolError("VOICE_MANIFEST_RESOURCE_UNAVAILABLE")
        manifest["providers"]["tts"] = {
            "options": options,
            "server_revision": config.tts_server_revision,
            "configuration": _file_digest(config.tts_service_config),
            "service": service_manifest(service),
        }
    if config.asr is not None:
        root = config.asr.model_dir
        try:
            if not root.is_dir() or root.is_symlink():
                raise OSError
            files = sorted(path for path in root.iterdir() if path.is_file())
            names = {path.name for path in files}
            if not {"model.bin", "config.json", "tokenizer.json"} <= names or len(files) > 64:
                raise OSError
        except OSError:
            raise VoiceProtocolError("VOICE_MANIFEST_RESOURCE_UNAVAILABLE") from None
        versions = {}
        for package in ("faster-whisper", "ctranslate2", "av"):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = "unavailable"
        manifest["providers"]["asr"] = {
            "options": config.asr.model_dump(mode="json", exclude={"model_dir"}),
            "versions": versions,
            "files": {path.name: _file_digest(path) for path in files},
        }
    return manifest


class VoiceNodeWorker:
    def __init__(self, config: VoiceNodeConfig, *, transport=None, supervisor_factory=None):
        self.config = config
        self.transport = transport
        self.supervisor_factory = supervisor_factory or ProcessSupervisor
        self.manifest_sha256 = None
        self._lease_until = 0.0

    async def _pin_core(self, request):
        """Reuse Host/SNI destination pinning without changing the HTTPS authority."""
        if self.config.core_connect_address is None:
            return
        origin = httpx.URL(self.config.core_origin)
        if (request.url.scheme, request.url.host, request.url.port) != (
            origin.scheme, origin.host, origin.port
        ):
            raise VoiceProtocolError("VOICE_CORE_ORIGIN_CHANGED")
        request.headers["Host"] = urlsplit(self.config.core_origin).netloc
        request.extensions["sni_hostname"] = origin.host
        request.url = request.url.copy_with(host=self.config.core_connect_address)

    def _verify(self, lease: SignedVoiceLease, *, rtt: float, previous=None) -> float:
        verify_voice_lease(
            lease,
            bytes.fromhex(self.config.core_public_key),
            node_id=self.config.node_id,
            manifest_sha256=self.manifest_sha256,
        )
        if previous is not None:
            fields = (
                "job_id",
                "generation",
                "node_id",
                "operation",
                "lease_token",
                "input_sha256",
                "input_bytes",
                "input_mime",
                "source_partial",
                "manifest_sha256",
                "issued_at",
                "job_deadline_at",
                "incarnation", "claim_sequence", "stop_token", "privacy_epoch",
            )
            if any(getattr(lease, name) != getattr(previous, name) for name in fields) or (
                lease.server_time < previous.server_time
            ):
                raise VoiceProtocolError("VOICE_LEASE_RENEWAL_MISMATCH")
        now = time.time()
        if lease.server_time.timestamp() > now + 5:
            raise VoiceProtocolError("VOICE_NODE_CLOCK_SKEW")
        remaining = min(
            lease.lease_expires_at.timestamp() - now,
            (lease.lease_expires_at - lease.server_time).total_seconds() - rtt,
        )
        if not math.isfinite(remaining) or remaining <= 0:
            raise VoiceProtocolError("VOICE_LEASE_EXPIRED")
        return time.monotonic() + remaining

    async def _request(self, client, method, suffix, value, model, *, budget_seconds=None,
                       owner_token=None, recovery_missing=False):
        data = canonical_bytes(value) if value is not None else b""
        if len(data) > MAX_RESULT_BODY_BYTES:
            raise VoiceProtocolError("VOICE_CORE_REQUEST_TOO_LARGE")
        headers = {"Content-Type": "application/json"}
        if owner_token is not None:
            headers["Authorization"] = "Bearer " + owner_token
        if isinstance(value, VoiceClaimRequest):
            headers["Idempotency-Key"] = value.request_id
        elif method == "PUT":
            headers["Idempotency-Key"] = f"voice-result:{value.ref.job_id}:{value.ref.generation}"
        started = time.monotonic()
        try:
            async with asyncio.timeout(budget_seconds or self.config.request_timeout_seconds):
                async with client.stream(
                    method,
                    self.config.core_origin + ROUTE_PREFIX + suffix,
                    headers=headers,
                    content=data,
                ) as response:
                    if response.status_code == 404 and recovery_missing:
                        raw = json.loads(await self._read(response, 65536))
                        if isinstance(raw, dict) and raw.get("error") == "VOICE_JOB_NOT_FOUND":
                            raise VoiceProtocolError("VOICE_RECOVERY_JOB_ABSENT")
                    if response.status_code in {409, 410}:
                        error = json.loads(await self._read(response, 65536)).get("error")
                        if error in {"VOICE_CLAIM_RETIRED", "VOICE_CLAIM_EPOCH_RETIRED",
                                     "VOICE_NODE_RECOVERY_REQUIRED", "VOICE_CLAIM_SEQUENCE_GAP",
                                     "VOICE_LEASE_RETIRED", "VOICE_UPGRADE_REQUIRED"}:
                            raise VoiceProtocolError(error)
                    self._check_response(response, "application/json")
                    raw = await self._read(response, 65536)
            return model.model_validate_json(raw), time.monotonic() - started
        except (httpx.HTTPError, TimeoutError):
            raise VoiceProtocolError("VOICE_CORE_UNAVAILABLE") from None
        except ValueError as error:
            if isinstance(error, VoiceProtocolError):
                raise
            raise VoiceProtocolError("VOICE_CORE_RESPONSE_INVALID") from None

    @staticmethod
    def _check_response(response, mime):
        if response.status_code in {401, 403}:
            raise VoiceProtocolError("VOICE_NODE_UNAUTHORIZED")
        if response.status_code in {409, 410}:
            raise VoiceProtocolError("VOICE_CORE_LEASE_REJECTED")
        if response.status_code != 200:
            raise VoiceProtocolError("VOICE_CORE_UNAVAILABLE")
        if (
            response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != mime
            or response.headers.get("content-encoding", "identity").strip().lower() != "identity"
        ):
            raise VoiceProtocolError("VOICE_CORE_RESPONSE_INVALID")

    @staticmethod
    async def _read(response, limit):
        length = response.headers.get("content-length")
        if length is not None and (
            not length.isascii()
            or not length.isdecimal()
            or len(length) > 10
            or int(length) > limit
        ):
            raise VoiceProtocolError("VOICE_CORE_RESPONSE_TOO_LARGE")
        data = bytearray()
        async for part in response.aiter_bytes(chunk_size=65536):
            if len(data) + len(part) > limit:
                raise VoiceProtocolError("VOICE_CORE_RESPONSE_TOO_LARGE")
            data.extend(part)
        if length is not None and len(data) != int(length):
            raise VoiceProtocolError("VOICE_CORE_RESPONSE_INVALID")
        return bytes(data)

    async def _control(
        self, client, state, lease, phase, *, result_sha=None, error_code=None, budget_seconds=None
    ):
        body = VoiceControlRequest(
            ref=VoiceLeaseRef.from_lease(lease),
            phase=phase,
            resource_state=state.resource()["state"],
            result_sha256=result_sha,
            error_code=error_code,
        )
        response, rtt = await self._request(
            client,
            "POST",
            f"/jobs/{lease.job_id}/control",
            body,
            VoiceControlResponse,
            budget_seconds=budget_seconds,
        )
        if response.action == "CONTINUE":
            self._lease_until = self._verify(response.renewed_lease, rtt=rtt, previous=lease)
            state.update_lease(response.renewed_lease)
            lease = response.renewed_lease
        return response, lease

    async def _input(self, client, lease):
        remaining = self._lease_until - time.monotonic()
        if remaining <= 0:
            raise VoiceProtocolError("VOICE_LEASE_EXPIRED")
        try:
            async with asyncio.timeout(min(remaining, self.config.request_timeout_seconds)):
                async with client.stream(
                    "POST",
                    self.config.core_origin + ROUTE_PREFIX + f"/jobs/{lease.job_id}/input",
                    headers={"Content-Type": "application/json"},
                    content=canonical_bytes(VoiceLeaseRef.from_lease(lease)),
                ) as response:
                    self._check_response(response, lease.input_mime)
                    data = await self._read(response, lease.input_bytes)
        except (httpx.HTTPError, TimeoutError):
            raise VoiceProtocolError("VOICE_CORE_UNAVAILABLE") from None
        if len(data) != lease.input_bytes or hashlib.sha256(data).hexdigest() != lease.input_sha256:
            raise VoiceProtocolError("VOICE_INPUT_DIGEST_MISMATCH")
        if lease.operation == "tts":
            if self.config.tts is None:
                raise VoiceProtocolError("VOICE_PROVIDER_NOT_CONFIGURED")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise VoiceProtocolError("VOICE_INPUT_TEXT_INVALID") from None
            if not text.strip() or len(text) > self.config.tts.max_text_chars:
                raise VoiceProtocolError("VOICE_INPUT_TEXT_INVALID")
        else:
            if self.config.asr is None:
                raise VoiceProtocolError("VOICE_PROVIDER_NOT_CONFIGURED")
            validate_wav(data, self.config.asr.input)
        return data

    def _packet(self, lease, data):
        provider = self.config.tts if lease.operation == "tts" else self.config.asr
        options = provider.model_dump(mode="json")
        if lease.operation == "tts" and provider.api_key is not None:
            options["api_key"] = provider.api_key.get_secret_value()
        return {
            "operation": lease.operation,
            "input_base64": base64.b64encode(data).decode(),
            "source_partial": lease.source_partial,
            "provider": options,
        }

    def _startup(self, state):
        resource = state.resource()
        if resource["state"] == "RUNNING":
            if resource.get("operation") == "asr" and process_has_exited(
                resource.get("pid"),
                resource.get("process_identity"),
            ):
                state.release_resource()
            else:
                state.quarantine("VOICE_NODE_RESTARTED_DURING_EXECUTION")
        job = state.active_job()
        if job and job["phase"] in {"CLAIMED", "CANCELLED"} and job["temp_json"]:
            # A restart after prepare/spawn must not overwrite the exact ownership
            # record or create another child while the first exit is unproven.
            state.phase(state.stop_ref(job), "CANCELLED",
                        error_code="VOICE_NODE_RESTARTED_BEFORE_DISPATCH")
            job = state.active_job()
            if not state.cleanup_receipt(job).temp_removed:
                state.quarantine("VOICE_PROCESS_EXIT_UNCONFIRMED")
        if job and job["phase"] == "RUNNING":
            state.phase(
                state.stop_ref(job),
                "UNKNOWN",
                error_code="VOICE_NODE_RESTARTED_DURING_EXECUTION",
            )

    async def _claim(self, client, state):
        session, _ = await self._request(client, "GET", "/session", None, VoiceSession)
        state.bind_session(session)
        request = state.next_claim(self.config.node_id, self.manifest_sha256)
        if (
            request.node_id != self.config.node_id
            or request.manifest_sha256 != self.manifest_sha256
        ):
            raise VoiceProtocolError("VOICE_PENDING_CONFIG_CHANGED")
        try:
            response, rtt = await self._request(
                client, "POST", "/claim", request, VoiceClaimResponse,
            )
        except VoiceProtocolError as error:
            if error.code == "VOICE_CLAIM_RETIRED" and state.active_job() is None:
                state.clear_claim()
                return None, 1
            raise
        if response.lease and (response.lease.incarnation != request.incarnation
                               or response.lease.claim_sequence != request.sequence):
            raise VoiceProtocolError("VOICE_CLAIM_REPLAY_MISMATCH")
        if response.lease is None:
            state.clear_claim()
            return None, response.retry_after_seconds
        try:
            self._lease_until = self._verify(response.lease, rtt=rtt)
        except VoiceProtocolError as error:
            if error.code != "VOICE_LEASE_EXPIRED":
                raise
            # A valid, expired replay must consume the pending claim key without
            # ever dispatching this generation. Otherwise an ACK loss can strand
            # the node on the same expired claim response indefinitely.
            job = state.accept_claim(response.lease)
            if job["phase"] == "CLAIMED":
                state.phase(response.lease, "CANCELLED", error_code="VOICE_LEASE_EXPIRED")
            return state.job(response.lease.job_id, response.lease.generation), 1
        job = state.accept_claim(response.lease)
        if state.resource()["state"] != "READY":
            state.phase(response.lease, "FAILED", error_code="VOICE_GPU_QUARANTINED")
            job = state.job(response.lease.job_id, response.lease.generation)
        return job, 1

    async def _stop_cleanup(self, client, state, job, *, recovery_missing=False):
        ref = state.stop_ref(job)
        for _ in range(2):  # An erasure epoch may have advanced since this lease was issued.
            job = state.job(ref.job_id, ref.generation)
            cleanup = state.cleanup_receipt(job)
            body = VoiceStopRequest(ref=ref, resource_state=state.resource()["state"],
                outcome=job["terminal_outcome"] or "UNKNOWN", cleanup=cleanup,
                cleanup_epoch=job["cleanup_epoch"])
            response, _ = await self._request(client, "POST", f"/jobs/{ref.job_id}/stop",
                                               body, VoiceStopResponse,
                                               recovery_missing=recovery_missing)
            state.acknowledge_cleanup(job, response)
            if state.job(ref.job_id, ref.generation) is None:
                return "DISCARDED"
            if response.cleanup_epoch == body.cleanup_epoch or not cleanup.complete():
                break
        return "RETRY"

    async def recover_claims(self, state, *, owner_token, operator, service_instance, confirmation):
        refs = state.prepare_owner_recovery()
        old_cursor = state.meta("claim_cursor")
        async with httpx.AsyncClient(
            transport=self.transport, trust_env=False, follow_redirects=False,
            event_hooks={"request": [self._pin_core]},
            timeout=self.config.request_timeout_seconds,
            headers={"Authorization": "Bearer " + self.config.credential.get_secret_value(),
                     "Accept-Encoding": "identity", "Accept": "application/json"},
        ) as client:
            initial, _ = await self._request(client, "GET", "/session", None, VoiceSession)
            body = VoiceRecovery(incarnation=initial.incarnation, operator=operator,
                                 service_instance=service_instance, confirmation=confirmation)
            recovered, _ = await self._request(
                client, "POST", f"/nodes/{self.config.node_id}/recover", body, VoiceSession,
                owner_token=owner_token,
            )
            if recovered.incarnation != initial.incarnation or recovered.recovery_required:
                raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
            missing = []
            for ref in refs:
                try:
                    outcome = await self._stop_cleanup(
                        client, state, state.job(ref.job_id, ref.generation), recovery_missing=True,
                    )
                except VoiceProtocolError as error:
                    if error.code != "VOICE_RECOVERY_JOB_ABSENT":
                        raise
                    missing.append(ref)
                else:
                    if outcome != "DISCARDED":
                        raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
            final, _ = await self._request(client, "GET", "/session", None, VoiceSession)
            if (final.incarnation != recovered.incarnation
                or final.last_sequence != recovered.last_sequence or final.recovery_required):
                raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
            state.complete_owner_recovery(missing, final, node_id=self.config.node_id,
                old_cursor=old_cursor, operator=operator, service_instance=service_instance,
                confirmation=confirmation)

    async def _upload(self, client, state, job, lease):
        result = state.cached_result(job)
        if result is None:
            state.phase(lease, "RESULT_LOST", error_code="VOICE_RESULT_CACHE_MISSING")
            return await self._stop_cleanup(
                client, state, state.job(lease.job_id, lease.generation),
            )
        if result.ref != VoiceLeaseRef.from_lease(lease):
            raise VoiceProtocolError("VOICE_RESULT_CACHE_INVALID")
        try:
            response, lease = await self._control(client, state, lease, "RESULT_READY",
                                                   result_sha=result.result_sha256)
        except VoiceProtocolError as error:
            if error.code not in {"VOICE_LEASE_RETIRED", "VOICE_CORE_LEASE_REJECTED",
                                  "VOICE_NODE_UNAUTHORIZED"}:
                raise
            state.phase(lease, "CANCELLED", error_code=error.code)
            return await self._stop_cleanup(
                client, state, state.job(lease.job_id, lease.generation),
            )
        if response.action == "COMMITTED":
            if response.committed_result_sha256 != result.result_sha256:
                state.finish(lease, error_code="VOICE_RESULT_CONFLICT", outcome="RESULT_LOST")
                await self._stop_cleanup(client, state, state.job(lease.job_id, lease.generation))
                raise VoiceProtocolError("VOICE_RESULT_CONFLICT")
        elif response.action != "CONTINUE":
            state.phase(lease, "CANCELLED", error_code="VOICE_LEASE_" + response.action)
            return await self._stop_cleanup(
                client, state, state.job(lease.job_id, lease.generation),
            )
        else:
            remaining = self._lease_until - time.monotonic()
            if remaining <= 0:
                raise VoiceProtocolError("VOICE_LEASE_EXPIRED")
            try:
                receipt, _ = await self._request(client, "PUT", f"/jobs/{lease.job_id}/result",
                    result, VoiceResultReceipt,
                    budget_seconds=min(remaining, self.config.request_timeout_seconds))
            except VoiceProtocolError as error:
                if error.code != "VOICE_LEASE_RETIRED":
                    raise
                state.phase(lease, "CANCELLED", error_code=error.code)
                return await self._stop_cleanup(
                    client, state, state.job(lease.job_id, lease.generation),
                )
            if (receipt.job_id, receipt.generation, receipt.result_sha256) != (
                lease.job_id, lease.generation, result.result_sha256):
                raise VoiceProtocolError("VOICE_RESULT_RECEIPT_MISMATCH")
        state.finish(lease)
        outcome = await self._stop_cleanup(client, state, state.job(lease.job_id, lease.generation))
        return "COMPLETED" if outcome == "DISCARDED" else outcome

    def _validated_payload(self, message, lease, data):
        if set(message) != {"status", "payload"}:
            raise VoiceProtocolError("VOICE_PROCESS_OUTPUT_INVALID")
        try:
            payload = TypeAdapter(VoicePayload).validate_python(message["payload"])
        except (KeyError, ValueError):
            raise VoiceProtocolError("VOICE_PROCESS_OUTPUT_INVALID") from None
        if payload.kind != lease.operation:
            raise VoiceProtocolError("VOICE_PROCESS_OUTPUT_INVALID")
        if payload.kind == "tts":
            audio = validate_wav(payload.audio_bytes(), self.config.tts.output)
            if any(
                getattr(payload, name) != getattr(audio, name)
                for name in (
                    "sample_rate",
                    "channels",
                    "sample_width",
                    "frames",
                    "duration_seconds",
                )
            ):
                raise VoiceProtocolError("VOICE_PROCESS_OUTPUT_INVALID")
        elif (
            payload.partial != lease.source_partial
            or payload.duration_seconds
            != validate_wav(data, self.config.asr.input).duration_seconds
        ):
            raise VoiceProtocolError("VOICE_PROCESS_OUTPUT_INVALID")
        return payload

    async def _settle_cancel(self, client, state, lease, error_code):
        state.phase(lease, "CANCELLED", error_code=error_code)
        return await self._stop_cleanup(client, state, state.job(lease.job_id, lease.generation))

    async def _cancel_claim(self, client, state, lease, action, *, supervisor=None):
        if supervisor is not None:
            stopped = False
            try:
                stopped = await supervisor.stop()
            finally:
                if not stopped:
                    state.quarantine("VOICE_PROCESS_EXIT_UNCONFIRMED")
        error_code = "VOICE_LEASE_" + action
        state.phase(lease, "CANCELLED", error_code=error_code)
        return await self._settle_cancel(client, state, lease, error_code)

    async def _execute(self, client, state, lease):
        response, lease = await self._control(client, state, lease, "CLAIMED")
        if response.action != "CONTINUE":
            return await self._cancel_claim(client, state, lease, response.action)
        data = await self._input(client, lease)
        if digest(await asyncio.to_thread(local_manifest, self.config)) != self.manifest_sha256:
            raise VoiceProtocolError("VOICE_MANIFEST_CHANGED")
        temporary = state.prepare_temp(lease)
        supervisor = self.supervisor_factory(self.config.workspace, temporary)
        supervisor.on_started = lambda pid, identity: state.bind_process(lease, pid, identity)
        dispatched, cleared, execution = False, False, None
        try:
            remaining = self._lease_until - time.monotonic()
            if remaining <= 0:
                raise VoiceProtocolError("VOICE_LEASE_EXPIRED")
            await supervisor.start(min(remaining, self.config.startup_timeout_seconds))
            response, lease = await self._control(client, state, lease, "CLAIMED")
            if response.action != "CONTINUE":
                return await self._cancel_claim(
                    client, state, lease, response.action, supervisor=supervisor,
                )
            state.dispatch(lease, pid=supervisor.process.pid, process_identity=supervisor.identity)
            dispatched = True
            overall_deadline = time.monotonic() + self.config.execution_timeout_seconds
            await asyncio.wait_for(
                supervisor.dispatch(self._packet(lease, data)),
                max(0.001, min(self._lease_until, overall_deadline) - time.monotonic()),
            )
            execution = asyncio.create_task(supervisor.result())
            while not execution.done():
                remaining = min(self._lease_until, overall_deadline) - time.monotonic()
                if remaining <= 0:
                    raise VoiceProtocolError("VOICE_EXECUTION_DEADLINE")
                done, _ = await asyncio.wait(
                    {execution},
                    timeout=min(remaining, self.config.heartbeat_seconds),
                )
                if done:
                    break
                remaining = min(self._lease_until, overall_deadline) - time.monotonic()
                if remaining <= 0:
                    raise VoiceProtocolError("VOICE_EXECUTION_DEADLINE")
                response, lease = await self._control(
                    client,
                    state,
                    lease,
                    "RUNNING",
                    budget_seconds=min(remaining, self.config.request_timeout_seconds),
                )
                if response.action != "CONTINUE":
                    raise VoiceProtocolError("VOICE_LEASE_" + response.action)
            if time.monotonic() >= min(self._lease_until, overall_deadline):
                raise VoiceProtocolError("VOICE_EXECUTION_DEADLINE")
            message = execution.result()
            stopped = await supervisor.finish()
            if not stopped:
                raise VoiceProtocolError("VOICE_PROCESS_EXIT_UNCONFIRMED")
            if message.get("status") != "SUCCEEDED":
                if (
                    set(message) != {"status", "error_code", "remote_state"}
                    or message["status"] != "FAILED"
                    or message["remote_state"] not in {"NOT_STARTED", "UNKNOWN"}
                    or (
                        message["remote_state"] == "NOT_STARTED"
                        and message["error_code"] not in TTS_NOT_STARTED_CODES
                    )
                ):
                    raise VoiceProtocolError("VOICE_PROCESS_OUTPUT_INVALID")
                code = message.get("error_code", "VOICE_PROCESS_EXECUTION_FAILED")
                # Child messages are untrusted even though they come through a private pipe.
                VoiceControlRequest(
                    ref=VoiceLeaseRef.from_lease(lease),
                    phase="FAILED",
                    resource_state=state.resource()["state"],
                    error_code=code,
                )
                if lease.operation == "asr" or message["remote_state"] == "NOT_STARTED":
                    state.release_resource()
                    cleared = True
                raise VoiceProtocolError(code)
            payload = self._validated_payload(message, lease, data)
            state.release_resource()
            cleared = True
            result = make_voice_result(VoiceLeaseRef.from_lease(lease), payload)
            state.save_result(
                result,
                expires_at=min(
                    lease.job_deadline_at.timestamp(),
                    time.time() + self.config.result_cache_ttl_seconds,
                ),
            )
        finally:
            if execution is not None and not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
            stopped = await supervisor.stop()
            if stopped and supervisor.process is not None:
                state.process_stopped(lease, supervisor.process.pid, supervisor.identity)
            if dispatched and not cleared:
                if lease.operation == "tts" or not stopped:
                    state.quarantine("VOICE_PROVIDER_EXECUTION_UNCERTAIN")
                else:
                    state.release_resource()
                state.phase(lease, "UNKNOWN", error_code="VOICE_PROVIDER_EXECUTION_UNCERTAIN")
            state.cleanup()
        return await self._upload(client, state, state.job(lease.job_id, lease.generation), lease)

    async def _cycle(self, client, state):
        state.cleanup()
        pending = state.pending_cleanup()
        if pending:
            return await self._stop_cleanup(client, state, pending), 5
        job = state.active_job()
        retry_after = 1
        if job is None:
            job, retry_after = await self._claim(client, state)
        if job is None:
            return "IDLE", retry_after
        if job["lease_json"] is None:
            return await self._stop_cleanup(client, state, job), retry_after
        lease = SignedVoiceLease.model_validate_json(job["lease_json"])
        if job["phase"] == "RESULT_READY":
            return await self._upload(client, state, job, lease), retry_after
        if job["phase"] != "CLAIMED":
            raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
        if state.resource()["state"] != "READY":
            state.phase(lease, "FAILED", error_code="VOICE_GPU_QUARANTINED")
            return "RETRY", retry_after
        try:
            return await self._execute(client, state, lease), retry_after
        except Exception as error:
            code = getattr(error, "code", "VOICE_NODE_EXECUTION_FAILED")
            current = state.job(lease.job_id, lease.generation)
            if code in {"VOICE_LEASE_RETIRED", "VOICE_CORE_LEASE_REJECTED"}:
                if current["lease_json"] is not None:
                    state.phase(lease, "CANCELLED", error_code=code)
                return await self._stop_cleanup(
                    client, state, state.job(lease.job_id, lease.generation),
                ), retry_after
            if current["phase"] in {"CLAIMED", "RUNNING"} and code != "VOICE_CORE_UNAVAILABLE":
                state.phase(lease, "FAILED", error_code=code)
            raise VoiceProtocolError(code) from None

    async def run(self, *, once: bool = False) -> str:
        with NodeState(self.config.state_directory) as state:
            self._startup(state)
            self.manifest_sha256 = digest(await asyncio.to_thread(local_manifest, self.config))
            async with httpx.AsyncClient(
                transport=self.transport,
                event_hooks={"request": [self._pin_core]},
                timeout=self.config.request_timeout_seconds,
                trust_env=False,
                follow_redirects=False,
                headers={
                    "Authorization": "Bearer " + self.config.credential.get_secret_value(),
                    "Accept-Encoding": "identity",
                    "Accept": "application/json",
                },
            ) as client:
                while True:
                    try:
                        outcome, retry_after = await self._cycle(client, state)
                    except VoiceProtocolError as error:
                        state.set_meta("last_error", {"code": error.code, "at_unix": time.time()})
                        if once:
                            raise
                        if error.code in {
                            "VOICE_NODE_UNAUTHORIZED",
                            "VOICE_STATE_RECOVERY_REQUIRED", "VOICE_NODE_RECOVERY_REQUIRED",
                            "VOICE_CLAIM_EPOCH_RETIRED", "VOICE_UPGRADE_REQUIRED",
                        }:
                            raise
                        outcome, retry_after = "RETRY", 5
                    if once:
                        return outcome
                    await asyncio.sleep(retry_after)


def main(argv=None, *, transport=None, supervisor_factory=None) -> int:
    parser = argparse.ArgumentParser(description="Outbound local voice worker")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--print-manifest", action="store_true")
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--recover-claims", action="store_true")
    parser.add_argument("--control-token-file", type=Path)
    parser.add_argument("--recover-resource", action="store_true")
    parser.add_argument("--quarantine-id")
    parser.add_argument("--operator")
    parser.add_argument("--service-instance")
    parser.add_argument("--confirmation")
    arguments = parser.parse_args(argv)
    try:
        with arguments.config.open("rb") as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise VoiceProtocolError("VOICE_NODE_CONFIG_TOO_LARGE")
        config = VoiceNodeConfig.model_validate_json(raw)
        if arguments.cleanup_only:
            with NodeState(config.state_directory) as state:
                print(json.dumps(state.cleanup(), sort_keys=True))
            return 0
        if arguments.recover_claims:
            if (not arguments.control_token_file
                or arguments.confirmation != "inflight-execution-stopped"
                or not arguments.operator or not arguments.service_instance):
                raise VoiceProtocolError("VOICE_RECOVERY_CONFIRMATION_REQUIRED")
            with NodeState(config.state_directory) as state:
                token = arguments.control_token_file.read_text(encoding="utf-8").strip()
                worker = VoiceNodeWorker(config, transport=transport)
                asyncio.run(worker.recover_claims(state, owner_token=token,
                    operator=arguments.operator, service_instance=arguments.service_instance,
                    confirmation=arguments.confirmation))
            return 0
        if arguments.print_manifest:
            print(digest(local_manifest(config)))
            return 0
        if arguments.recover_resource:
            with NodeState(config.state_directory) as state:
                state.recover_manually(
                    quarantine_id=arguments.quarantine_id or "",
                    operator=arguments.operator or "",
                    service_instance=arguments.service_instance or "",
                    confirmation=arguments.confirmation or "",
                )
            return 0
        worker = VoiceNodeWorker(config, transport=transport, supervisor_factory=supervisor_factory)
        asyncio.run(worker.run(once=arguments.once))
        return 0
    except (VoiceProtocolError, ValueError, OSError) as error:
        print(getattr(error, "code", "VOICE_NODE_CONFIG_INVALID"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
