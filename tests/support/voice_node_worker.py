import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion.voice_protocol import (
    ROUTE_PREFIX,
    VoiceClaimRequest,
    VoiceControlRequest,
    VoiceLease,
    VoiceResult,
    VoiceStopRequest,
    canonical_bytes,
    sign_voice_lease,
)
from companion_voice_node.providers import FasterWhisperConfig, OpenAISpeechConfig
from companion_voice_node.state import NodeState
from companion_voice_node.supervisor import ProcessSupervisor
from companion_voice_node.worker import VoiceNodeConfig
from tests.voice_service_fixtures import speech_config, wav_bytes

WORKSPACE = Path(__file__).resolve().parents[2]


@pytest.fixture
def node(tmp_path):
    key = Ed25519PrivateKey.generate()
    model = tmp_path / "asr-model"
    model.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (model / name).write_bytes(b"model stub for supervised process tests")
    service = speech_config(tmp_path / "speech")
    service_path = tmp_path / "speech.json"
    service_path.write_text(service.model_dump_json(), encoding="utf-8")
    config = VoiceNodeConfig(
        node_id="test-local-node",
        core_origin="https://core.invalid",
        credential="dedicated-node-credential-" + "x" * 32,
        core_public_key=key.public_key().public_bytes_raw().hex(),
        workspace=WORKSPACE,
        state_directory=tmp_path / "node-state",
        tts=OpenAISpeechConfig(model=service.model),
        asr=FasterWhisperConfig(model_dir=model),
        tts_service_config=service_path,
        tts_server_revision="a" * 40,
        heartbeat_seconds=0.05,
        request_timeout_seconds=0.3,
        execution_timeout_seconds=5,
        startup_timeout_seconds=5,
    )
    return config, key, tmp_path / "child-receipts.jsonl"


def child_receipts(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class FixtureSupervisor(ProcessSupervisor):
    def __init__(self, workspace, temporary, *, counter, scenario="success"):
        super().__init__(
            workspace,
            temporary,
            command=(
                sys.executable,
                "-B",
                "-m",
                "tests.support.voice_child",
                str(os.getpid()),
            ),
        )
        self.counter, self.scenario = counter, scenario

    async def dispatch(self, packet):
        assert "credential" not in packet and "core_origin" not in packet
        await super().dispatch(
            packet
            | {
                "_fixture": {
                    "counter": str(self.counter),
                    "scenario": self.scenario,
                }
            }
        )


def factory(counter, scenario="success"):
    return lambda workspace, temporary: FixtureSupervisor(
        workspace,
        temporary,
        counter=counter,
        scenario=scenario,
    )


class FakeCore:
    """Only the remote transport is mocked; the worker and provider process are real."""

    def __init__(self, config, key, operation="tts"):
        self.config, self.key, self.operation = config, key, operation
        self.data = "仅限内存的测试文本".encode() if operation == "tts" else wav_bytes()
        self.issued = datetime.now(UTC)
        self.job_id = "voice_" + "a" * 32
        self.requests, self.claim_bodies, self.result_bodies, self.controls = [], [], [], []
        self.claim_loss = False
        self.result_loss = None
        self.accepted_digest = None
        self.invalid_lease = None
        self.corrupt_input = False
        self.running_action = None
        self.result_action = None
        self.control_failure = False
        self.wrong_receipt = False
        self.claim_manifest = None
        self.generation = 1
        self.incarnation = "b" * 32
        self.last_sequence = 0
        self.replay = None
        self.last_claim = None
        self.stops = []
        self.stopped = False
        self.cleanup_epoch = 0

    def lease(self, manifest):
        now = datetime.now(UTC)
        if self.invalid_lease == "expired":
            now = now - timedelta(seconds=60)
        values = {
            "incarnation": self.incarnation,
            "claim_sequence": max(1, self.last_sequence),
            "stop_token": "s" * 48,
            "privacy_epoch": 0,
            "job_id": self.job_id,
            "generation": self.generation,
            "node_id": self.config.node_id,
            "operation": self.operation,
            "lease_token": "t" * 48,
            "input_sha256": hashlib.sha256(self.data).hexdigest(),
            "input_bytes": len(self.data),
            "input_mime": "text/plain" if self.operation == "tts" else "audio/wav",
            "source_partial": self.operation == "asr",
            "manifest_sha256": manifest,
            "issued_at": min(self.issued, now),
            "server_time": now,
            "lease_expires_at": now + timedelta(seconds=15),
            "job_deadline_at": self.issued + timedelta(seconds=120),
        }
        if self.invalid_lease == "manifest":
            values["manifest_sha256"] = "0" * 64
        signed = sign_voice_lease(VoiceLease(**values), self.key)
        if self.invalid_lease == "signature":
            signed = signed.model_copy(update={"signature": "0" * 128})
        return signed.model_dump(mode="json")

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.scheme == "https" and request.url.host == "core.invalid"
        if (
            request.headers.get("authorization")
            != "Bearer " + self.config.credential.get_secret_value()
        ):
            return httpx.Response(403, json={"private": "wrong auth"})
        assert request.headers["accept-encoding"] == "identity"
        suffix = request.url.path.removeprefix(ROUTE_PREFIX)
        if suffix == "/session":
            return httpx.Response(200, json={"incarnation": self.incarnation,
                "last_sequence": self.last_sequence, "replay_available": self.replay is not None,
                "recovery_required": False})
        if suffix == "/claim":
            claim = VoiceClaimRequest.model_validate_json(request.content)
            self.claim_bodies.append(request.content)
            assert request.headers["idempotency-key"] == claim.request_id
            self.claim_manifest = claim.manifest_sha256
            if claim.incarnation != self.incarnation:
                return httpx.Response(410, json={"error": "VOICE_CLAIM_EPOCH_RETIRED"})
            if claim.sequence < self.last_sequence:
                return httpx.Response(410, json={"error": "VOICE_CLAIM_RETIRED"})
            if claim.sequence > self.last_sequence + 1:
                return httpx.Response(409, json={"error": "VOICE_CLAIM_SEQUENCE_GAP"})
            if claim.sequence == self.last_sequence:
                if self.last_claim != request.content:
                    return httpx.Response(409, json={"error": "VOICE_CLAIM_REPLAY_MISMATCH"})
                if self.replay is None:
                    return httpx.Response(410, json={"error": "VOICE_CLAIM_RETIRED"})
            else:
                self.last_sequence = claim.sequence
                self.last_claim = request.content
                unavailable = self.stopped or claim.resource_state == "QUARANTINED"
                self.replay = {"lease": None if unavailable
                               else self.lease(claim.manifest_sha256)}
            if self.claim_loss:
                self.claim_loss = False
                raise httpx.ReadError("claim response lost", request=request)
            return httpx.Response(200, json=self.replay)
        if suffix.endswith("/stop"):
            body = VoiceStopRequest.model_validate_json(request.content)
            self.stops.append(body)
            assert body.ref.job_id == self.job_id and body.ref.stop_token == "s" * 48
            closed = body.resource_state == "READY"
            confirmed = body.cleanup.complete() and body.cleanup_epoch == self.cleanup_epoch
            if closed:
                self.stopped = True
                self.replay = None
            return httpx.Response(200, json={"custody_closed": closed,
                "cleanup_confirmed": confirmed, "cleanup_epoch": self.cleanup_epoch})
        if suffix.endswith("/control"):
            body = VoiceControlRequest.model_validate_json(request.content)
            self.controls.append(body)
            assert body.ref.job_id == self.job_id and body.ref.lease_token == "t" * 48
            if self.accepted_digest:
                return httpx.Response(
                    200,
                    json={
                        "action": "COMMITTED",
                        "committed_result_sha256": self.accepted_digest,
                    },
                )
            if body.phase in {"FAILED", "UNKNOWN", "CANCELLED", "RESULT_LOST"}:
                return httpx.Response(200, json={"action": "CANCEL"})
            if body.phase == "RESULT_READY" and self.result_action:
                return httpx.Response(200, json={"action": self.result_action})
            if body.phase == "RUNNING":
                if self.control_failure:
                    raise httpx.ReadError("private control failure", request=request)
                if self.running_action:
                    return httpx.Response(200, json={"action": self.running_action})
            return httpx.Response(
                200,
                json={
                    "action": "CONTINUE",
                    "renewed_lease": self.lease(body.ref.manifest_sha256),
                },
            )
        if suffix.endswith("/input"):
            data = self.data
            if self.corrupt_input:
                data = data[:-1] + bytes([data[-1] ^ 1])
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/plain; charset=utf-8"
                    if self.operation == "tts"
                    else "audio/wav",
                },
                content=data,
            )
        if suffix.endswith("/result"):
            result = VoiceResult.model_validate_json(request.content)
            self.result_bodies.append(request.content)
            assert (
                request.headers["idempotency-key"]
                == f"voice-result:{self.job_id}:{self.generation}"
            )
            if self.result_loss == "before-commit":
                self.result_loss = None
                raise httpx.ReadError("result transport failed", request=request)
            if self.accepted_digest and self.accepted_digest != result.result_sha256:
                return httpx.Response(409, json={"error": "different digest"})
            self.accepted_digest = result.result_sha256
            if self.result_loss == "after-commit":
                self.result_loss = None
                raise httpx.ReadError("result acknowledgment lost", request=request)
            return httpx.Response(
                200,
                json={
                    "job_id": self.job_id,
                    "generation": self.generation,
                    "result_sha256": "0" * 64 if self.wrong_receipt else result.result_sha256,
                },
            )
        raise AssertionError("Unexpected route")


def write_config(path, config):
    path.write_bytes(
        canonical_bytes(
            config.model_dump(mode="json")
            | {
                "credential": config.credential.get_secret_value(),
            }
        )
    )


def publish_parent_ready(path, value):
    pending = path.with_suffix(".part")
    try:
        pending.write_text(json.dumps(value), encoding="utf-8")
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


async def parent_fixture(spec_path):
    spec = json.loads(await asyncio.to_thread(Path(spec_path).read_bytes))
    config = VoiceNodeConfig.model_validate(spec["config"])
    lease = sign_voice_lease(
        VoiceLease(**spec["lease"]),
        Ed25519PrivateKey.from_private_bytes(bytes.fromhex(spec["key"])),
    )
    counter = Path(spec["counter"])
    with NodeState(config.state_directory) as state:
        state.accept_claim(lease)
        supervisor = FixtureSupervisor(
            config.workspace,
            state.prepare_temp(lease),
            counter=counter,
            scenario="block",
        )
        await supervisor.start(5)
        state.bind_process(lease, supervisor.process.pid, supervisor.identity)
        state.dispatch(lease, pid=supervisor.process.pid, process_identity=supervisor.identity)
        await supervisor.dispatch(
            {
                "operation": lease.operation,
                "provider": {},
                "input_base64": "",
                "source_partial": False,
            }
        )
        await asyncio.to_thread(
            publish_parent_ready,
            Path(spec["ready"]),
            {"pid": supervisor.process.pid, "identity": supervisor.identity},
        )
        await asyncio.Event().wait()


if __name__ == "__main__":
    if sys.argv[1] == "--parent-fixture":
        asyncio.run(parent_fixture(sys.argv[2]))
