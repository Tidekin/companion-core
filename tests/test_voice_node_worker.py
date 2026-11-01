import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from companion.voice_protocol import (
    ROUTE_PREFIX,
    VoiceAudioResult,
    VoiceClaimRequest,
    VoiceControlRequest,
    VoiceLease,
    VoiceProtocolError,
    VoiceResult,
    VoiceStopRequest,
    canonical_bytes,
    digest,
    sign_voice_lease,
)
from companion_voice_node.providers import FasterWhisperConfig, OpenAISpeechConfig, validate_wav
from companion_voice_node.state import NodeState
from companion_voice_node.supervisor import ProcessSupervisor, child_entry, process_has_exited
from companion_voice_node.worker import VoiceNodeConfig, VoiceNodeWorker, local_manifest, main
from tests.test_local_voice_providers import wav_bytes
from tests.voice_service_fixtures import speech_config

WORKSPACE = Path(__file__).resolve().parents[1]


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


def fixture_provider(packet):
    fixture = packet.pop("_fixture")
    with Path(fixture["counter"]).open("a", encoding="utf-8") as output:
        output.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "environment_keys": sorted(os.environ),
                    "packet_keys": sorted(packet),
                    "provider_keys": sorted(packet["provider"]),
                }
            )
            + "\n"
        )
    if fixture["scenario"] == "block":
        time.sleep(60)
        raise RuntimeError("blocked fixture should have been terminated")
    if fixture["scenario"] == "crash":
        os._exit(3)
    if fixture["scenario"] == "bad-result":
        return {"status": "SUCCEEDED", "payload": {"kind": "tts", "private": "do not echo"}}
    if fixture["scenario"] == "false-not-started":
        return {"status": "FAILED", "remote_state": "NOT_STARTED", "error_code": "UNPROVEN_FAILURE"}
    if packet["operation"] == "tts":
        audio = validate_wav(
            wav_bytes(45, rate=44100) if fixture["scenario"] == "large-result" else wav_bytes(0.125)
        )
        payload = VoiceAudioResult(
            audio_base64=base64.b64encode(audio.audio).decode(),
            audio_sha256=audio.sha256,
            sample_rate=audio.sample_rate,
            channels=1,
            sample_width=2,
            frames=audio.frames,
            duration_seconds=audio.duration_seconds,
        ).model_dump(mode="json")
    else:
        audio = validate_wav(base64.b64decode(packet["input_base64"]))
        payload = {
            "kind": "asr",
            "text": "测试结果",
            "segments": [
                {
                    "start_seconds": 0.0,
                    "end_seconds": audio.duration_seconds,
                    "text": "测试结果",
                }
            ],
            "language": "zh",
            "language_probability": 0.9,
            "duration_seconds": audio.duration_seconds,
            "partial": packet["source_partial"],
        }
    return {"status": "SUCCEEDED", "payload": payload}


class FixtureSupervisor(ProcessSupervisor):
    def __init__(self, workspace, temporary, *, counter, scenario="success"):
        super().__init__(
            workspace,
            temporary,
            command=(
                sys.executable,
                "-B",
                "-m",
                "tests.test_voice_node_worker",
                "--child-fixture",
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


def run_once(config, core, counter, scenario="success"):
    worker = VoiceNodeWorker(
        config,
        transport=httpx.MockTransport(core),
        supervisor_factory=factory(counter, scenario),
    )
    return asyncio.run(asyncio.wait_for(worker.run(once=True), 15))


@pytest.mark.parametrize("operation", ["tts", "asr"])
def test_real_worker_and_separate_provider_process_complete_without_input_persistence(
    node, operation, monkeypatch
):
    config, key, counter = node
    monkeypatch.setenv("VOICE_NODE_CREDENTIAL", config.credential.get_secret_value())
    core = FakeCore(config, key, operation)
    assert run_once(config, core, counter) == "COMPLETED"
    receipts = child_receipts(counter)
    assert len(receipts) == 1 and receipts[0]["pid"] != os.getpid()
    assert "VOICE_NODE_CREDENTIAL" not in receipts[0]["environment_keys"]
    assert "credential" not in receipts[0]["provider_keys"]
    assert len(core.result_bodies) == 1
    result = VoiceResult.model_validate_json(core.result_bodies[0])
    if operation == "asr":
        assert result.payload.partial is True and result.payload.text == "测试结果"
    with NodeState(config.state_directory) as state:
        assert state.active_job() is None and state.resource()["state"] == "READY"
        assert [p.name for p in state.cache.iterdir()] == ["CACHEDIR.TAG"]
    assert not list((config.state_directory / "child-temp").iterdir())
    assert core.data not in (config.state_directory / "journal.sqlite").read_bytes()


@pytest.mark.parametrize("invalid", ["signature", "manifest"])
def test_invalid_signed_scope_never_launches_provider(node, invalid):
    config, key, counter = node
    core = FakeCore(config, key)
    core.invalid_lease = invalid
    with pytest.raises(VoiceProtocolError):
        run_once(config, core, counter)
    assert child_receipts(counter) == [] and len(core.requests) == 2


def test_expired_claim_replay_is_closed_without_dispatch_or_permanent_claim_retry(node):
    config, key, counter = node
    core = FakeCore(config, key)
    core.claim_loss = True
    with pytest.raises(VoiceProtocolError, match="VOICE_CORE_UNAVAILABLE"):
        run_once(config, core, counter)
    core.invalid_lease = "expired"
    core.replay = {"lease": core.lease(core.claim_manifest)}
    assert run_once(config, core, counter) == "DISCARDED"
    assert core.claim_bodies[0] == core.claim_bodies[1]
    assert child_receipts(counter) == []
    with NodeState(config.state_directory) as state:
        assert state.pending_claim() is None and state.active_job() is None
        assert state.job(core.job_id, 1) is None


def test_input_digest_mismatch_is_rejected_before_dispatch(node):
    config, key, counter = node
    core = FakeCore(config, key)
    core.corrupt_input = True
    with pytest.raises(VoiceProtocolError, match="VOICE_INPUT_DIGEST_MISMATCH"):
        run_once(config, core, counter)
    assert child_receipts(counter) == []
    with NodeState(config.state_directory) as state:
        assert state.active_job()["phase"] == "FAILED"


def test_claim_response_loss_replays_identical_key_and_body_across_restart(node):
    config, key, counter = node
    core = FakeCore(config, key)
    core.claim_loss = True
    with pytest.raises(VoiceProtocolError, match="VOICE_CORE_UNAVAILABLE"):
        run_once(config, core, counter)
    assert not child_receipts(counter)
    assert run_once(config, core, counter) == "COMPLETED"
    assert core.claim_bodies[0] == core.claim_bodies[1]
    assert len(child_receipts(counter)) == 1


@pytest.mark.parametrize("loss", ["before-commit", "after-commit"])
def test_result_retry_after_restart_never_reexecutes_provider(node, loss):
    config, key, counter = node
    core = FakeCore(config, key)
    core.result_loss = loss
    with pytest.raises(VoiceProtocolError, match="VOICE_CORE_UNAVAILABLE"):
        run_once(config, core, counter)
    with NodeState(config.state_directory) as state:
        assert state.active_job()["phase"] == "RESULT_READY"
        assert state.resource()["state"] == "READY"
        assert state.cached_result(state.active_job()) is not None
    assert run_once(config, core, counter) == "COMPLETED"
    assert len(child_receipts(counter)) == 1
    if loss == "before-commit":
        assert len(core.result_bodies) == 2 and core.result_bodies[0] == core.result_bodies[1]
    else:
        assert len(core.result_bodies) == 1


def test_duplicate_completed_generation_does_not_run_again(node):
    config, key, counter = node
    core = FakeCore(config, key)
    assert run_once(config, core, counter) == "COMPLETED"
    assert run_once(config, core, counter) == "IDLE"
    assert len(child_receipts(counter)) == 1 and len(core.result_bodies) == 1


@pytest.mark.parametrize("operation", ["tts", "asr"])
def test_cancel_stops_owned_child_and_preserves_tts_quarantine(node, operation):
    config, key, counter = node
    core = FakeCore(config, key, operation)
    core.running_action = "CANCEL"
    with pytest.raises(VoiceProtocolError, match="VOICE_LEASE_CANCEL"):
        run_once(config, core, counter, "block")
    receipt = child_receipts(counter)[0]
    with NodeState(config.state_directory) as state:
        resource = state.resource()
        assert resource["state"] == ("QUARANTINED" if operation == "tts" else "READY")
        if operation == "tts":
            assert process_has_exited(receipt["pid"], resource["process_identity"])
            quarantine_id = resource["quarantine_id"]
    assert run_once(config, core, counter) == ("RETRY" if operation == "tts" else "DISCARDED")
    if operation == "tts":
        assert run_once(config, core, counter) == "RETRY"
        with NodeState(config.state_directory) as state:
            assert state.resource()["quarantine_id"] == quarantine_id
            assert state.active_job()["phase"] == "UNKNOWN"
            state.recover_manually(quarantine_id=quarantine_id,
                operator="synthetic-operator", service_instance="synthetic-stopped-instance",
                confirmation="inflight-execution-stopped")
        assert run_once(config, core, counter) == "DISCARDED"
        assert len(core.claim_bodies) == 1
        assert core.stops[-1].resource_state == "READY"
    assert len(child_receipts(counter)) == 1 and core.result_bodies == []


def test_asr_native_process_is_killed_at_local_deadline_even_if_core_keeps_renewing(node):
    config, key, counter = node
    config = config.model_copy(update={"execution_timeout_seconds": 0.15})
    core = FakeCore(config, key, "asr")
    with pytest.raises(VoiceProtocolError, match="VOICE_EXECUTION_DEADLINE"):
        run_once(config, core, counter, "block")
    with NodeState(config.state_directory) as state:
        assert state.resource()["state"] == "READY" and state.active_job()["phase"] == "UNKNOWN"
    assert core.result_bodies == []


def test_lost_control_connection_stops_asr_instead_of_running_offline(node):
    config, key, counter = node
    core = FakeCore(config, key, "asr")
    core.control_failure = True
    with pytest.raises(VoiceProtocolError, match="VOICE_CORE_UNAVAILABLE"):
        run_once(config, core, counter, "block")
    with NodeState(config.state_directory) as state:
        assert state.resource()["state"] == "READY"
    assert len(child_receipts(counter)) == 1


def test_unknown_tts_execution_requires_explicit_recorded_manual_recovery(node):
    config, key, counter = node
    core = FakeCore(config, key)
    with pytest.raises(VoiceProtocolError, match="VOICE_PROCESS_OUTPUT_INVALID"):
        run_once(config, core, counter, "crash")
    with NodeState(config.state_directory) as state:
        quarantine_id = state.resource()["quarantine_id"]
        with pytest.raises(VoiceProtocolError, match="VOICE_RECOVERY_CONFIRMATION_REQUIRED"):
            state.recover_manually(
                quarantine_id=quarantine_id,
                operator="tester",
                service_instance="new-server",
                confirmation="healthy HTTP 200",
            )
        assert state.resource()["state"] == "QUARANTINED"
        state.recover_manually(
            quarantine_id=quarantine_id,
            operator="tester",
            service_instance="confirmed-new-server",
            confirmation="inflight-execution-stopped",
        )
        receipt = json.loads(
            state.db.execute("SELECT record_json FROM recovery_audit").fetchone()[0]
        )
        assert receipt["source"] == "manual_operator_confirmation"
        assert state.resource()["state"] == "READY"


def test_expired_result_cache_is_removed_without_regenerating_audio(node):
    config, key, counter = node
    core = FakeCore(config, key)
    core.result_loss = "before-commit"
    with pytest.raises(VoiceProtocolError):
        run_once(config, core, counter)
    with NodeState(config.state_directory) as state:
        with state.db:
            state.db.execute("UPDATE jobs SET cache_expires=?", (time.time() - 1,))
    assert run_once(config, core, counter) == "DISCARDED"
    assert len(child_receipts(counter)) == 1
    assert core.stops[-1].outcome == "RESULT_LOST"
    assert [p.name for p in (config.state_directory / "result-cache").iterdir()] == ["CACHEDIR.TAG"]


def test_receipt_digest_mismatch_does_not_accept_or_recompute_result(node):
    config, key, counter = node
    core = FakeCore(config, key)
    core.wrong_receipt = True
    with pytest.raises(VoiceProtocolError, match="VOICE_RESULT_RECEIPT_MISMATCH"):
        run_once(config, core, counter)
    with NodeState(config.state_directory) as state:
        assert state.active_job()["phase"] == "RESULT_READY"
    assert run_once(config, core, counter) == "COMPLETED"
    assert len(child_receipts(counter)) == 1


def test_local_manifest_hash_tracks_real_files_and_parameters(node):
    config, _, _ = node
    initial = digest(local_manifest(config))
    service = json.loads(config.tts_service_config.read_text(encoding="utf-8"))
    Path(service["model_path"]).write_bytes(b"changed local weights")
    assert digest(local_manifest(config)) != initial
    manifest = canonical_bytes(local_manifest(config))
    assert service["base"]["reference_audio_path"].encode() not in manifest
    assert config.credential.get_secret_value().encode() not in manifest


def test_manifest_change_after_claim_prevents_process_dispatch(node):
    config, key, counter = node
    core = FakeCore(config, key)

    def changed(request):
        response = core(request)
        if request.url.path.endswith("/input"):
            service = json.loads(config.tts_service_config.read_text(encoding="utf-8"))
            Path(service["model_path"]).write_bytes(b"changed during the claim")
        return response

    with pytest.raises(VoiceProtocolError, match="VOICE_MANIFEST_CHANGED"):
        run_once(config, changed, counter)
    assert child_receipts(counter) == []


def test_generation_change_during_inference_stops_child_and_cannot_publish(node):
    config, key, counter = node
    core = FakeCore(config, key, "asr")

    def renewed(request):
        response = core(request)
        if (
            request.url.path.endswith("/control")
            and json.loads(request.content)["phase"] == "RUNNING"
        ):
            body = response.json()
            unsigned = {k: v for k, v in body["renewed_lease"].items() if k != "signature"}
            unsigned["generation"] += 1
            body["renewed_lease"] = sign_voice_lease(VoiceLease(**unsigned), key).model_dump(
                mode="json"
            )
            return httpx.Response(200, json=body)
        return response

    with pytest.raises(VoiceProtocolError, match="VOICE_LEASE_RENEWAL_MISMATCH"):
        run_once(config, renewed, counter, "block")
    assert len(child_receipts(counter)) == 1 and core.result_bodies == []


def test_unproven_child_not_started_claim_cannot_clear_tts_quarantine(node):
    config, key, counter = node
    with pytest.raises(VoiceProtocolError, match="VOICE_PROCESS_OUTPUT_INVALID"):
        run_once(config, FakeCore(config, key), counter, "false-not-started")
    with NodeState(config.state_directory) as state:
        assert state.resource()["state"] == "QUARANTINED"


def test_multi_megabyte_result_crosses_real_pipe_with_bounded_json_upload(node):
    config, key, counter = node
    core = FakeCore(config, key)
    assert run_once(config, core, counter, "large-result") == "COMPLETED"
    assert 5_000_000 < len(core.result_bodies[0]) < 5_500_000
    result = VoiceResult.model_validate_json(core.result_bodies[0])
    assert result.payload.duration_seconds == 45
    assert len(result.payload.audio_bytes()) < 4_000_000


def test_cancellation_before_upload_discards_cached_result(node):
    config, key, counter = node
    core = FakeCore(config, key)
    core.result_action = "CANCEL"
    assert run_once(config, core, counter) == "DISCARDED"
    assert len(child_receipts(counter)) == 1 and core.result_bodies == []
    with NodeState(config.state_directory) as state:
        assert state.active_job() is None and not list(state.cache.glob("voice_*.json"))


def test_conflicting_committed_digest_is_terminal_and_never_regenerated(node):
    config, key, counter = node
    core = FakeCore(config, key)
    core.result_loss = "before-commit"
    with pytest.raises(VoiceProtocolError, match="VOICE_CORE_UNAVAILABLE"):
        run_once(config, core, counter)
    core.accepted_digest = "0" * 64
    with pytest.raises(VoiceProtocolError, match="VOICE_RESULT_CONFLICT"):
        run_once(config, core, counter)
    with NodeState(config.state_directory) as state:
        assert state.active_job() is None and not list(state.cache.glob("voice_*.json"))
    assert len(child_receipts(counter)) == 1


@pytest.mark.parametrize(
    "origin",
    [
        "http://core.invalid",
        "https://name:secret@core.invalid",
        "https://core.invalid/path",
        "https://core.invalid?next=https://outside.invalid",
        "https://core.invalid#fragment",
    ],
)
def test_node_config_requires_one_fixed_https_origin(node, origin):
    config, _, _ = node
    values = config.model_dump(mode="json") | {"credential": "c" * 48, "core_origin": origin}
    with pytest.raises(ValidationError):
        VoiceNodeConfig(**values)


def test_only_node_credential_is_used_and_rejection_prevents_dispatch(node):
    config, key, counter = node
    core = FakeCore(config, key)
    wrong = config.model_copy(
        update={"credential": type(config.credential)("owner-token-" + "x" * 48)}
    )
    with pytest.raises(VoiceProtocolError, match="VOICE_NODE_UNAUTHORIZED"):
        run_once(wrong, core, counter)
    assert child_receipts(counter) == []


def test_single_instance_lock_and_missing_journal_fail_closed(node):
    config, _, _ = node
    with NodeState(config.state_directory):
        with pytest.raises(VoiceProtocolError, match="VOICE_NODE_ALREADY_RUNNING"):
            with NodeState(config.state_directory):
                pass
    (config.state_directory / "journal.sqlite").unlink()
    with pytest.raises(VoiceProtocolError, match="VOICE_STATE_RECOVERY_REQUIRED"):
        with NodeState(config.state_directory):
            pass


def write_config(path, config):
    path.write_bytes(
        canonical_bytes(
            config.model_dump(mode="json")
            | {
                "credential": config.credential.get_secret_value(),
            }
        )
    )


def test_actual_cli_entry_uses_mock_http_and_real_child(node, tmp_path):
    config, key, counter = node
    path = tmp_path / "node-config.json"
    write_config(path, config)
    core = FakeCore(config, key)
    assert (
        main(
            ["--config", str(path), "--once"],
            transport=httpx.MockTransport(core),
            supervisor_factory=factory(counter),
        )
        == 0
    )
    assert len(child_receipts(counter)) == 1 and len(core.result_bodies) == 1


def publish_parent_ready(path, value):
    pending = path.with_suffix(".part")
    try:
        pending.write_text(json.dumps(value), encoding="utf-8")
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def test_parent_ready_is_hidden_until_written_and_published(tmp_path, monkeypatch):
    ready = tmp_path / "parent-ready.json"
    value = {"pid": 12345, "identity": "synthetic-child-identity"}
    writing, written = threading.Event(), threading.Event()
    finish_write, publish = threading.Event(), threading.Event()
    original_replace = Path.replace

    def delayed_write(path, text, *, encoding):
        with path.open("w", encoding=encoding) as output:
            writing.set()
            assert finish_write.wait(5), "writer was not released"
            return output.write(text)

    def delayed_replace(path, target):
        assert json.loads(path.read_text(encoding="utf-8")) == value
        written.set()
        assert publish.wait(5), "publication was not released"
        return original_replace(path, target)

    monkeypatch.setattr(Path, "write_text", delayed_write)
    monkeypatch.setattr(Path, "replace", delayed_replace)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(publish_parent_ready, ready, value)
        try:
            assert writing.wait(5), "writer did not open its file"
            assert not ready.exists(), "empty ready file became visible before JSON was written"
            finish_write.set()
            assert written.wait(5), "completed JSON did not reach publication"
            assert not ready.exists(), "ready became visible before atomic publication"
        finally:
            finish_write.set()
            publish.set()
        pending.result(timeout=5)
    assert json.loads(ready.read_text(encoding="utf-8")) == value
    assert sorted(path.name for path in tmp_path.iterdir()) == [ready.name]


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


@pytest.mark.parametrize("operation", ["asr", "tts"])
def test_parent_crash_contains_child_and_recovers_resource_conservatively(
    node, tmp_path, operation
):
    config, key, counter = node
    core = FakeCore(config, key, operation)
    ready, spec_path = tmp_path / "parent-ready.json", tmp_path / "parent-spec.json"
    spec_path.write_bytes(
        canonical_bytes(
            {
                "config": config.model_dump(mode="json")
                | {"credential": config.credential.get_secret_value()},
                "lease": {
                    k: v
                    for k, v in core.lease(digest(local_manifest(config))).items()
                    if k != "signature"
                },
                "key": key.private_bytes_raw().hex(),
                "counter": str(counter),
                "ready": str(ready),
            }
        )
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "tests.test_voice_node_worker",
            "--parent-fixture",
            str(spec_path),
        ],
        cwd=WORKSPACE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() or not child_receipts(counter):
            if process.poll() is not None or time.monotonic() > deadline:
                detail = (
                    process.stderr.read(4096).decode("utf-8", errors="replace")
                    if process.poll() is not None
                    else "startup deadline exceeded"
                )
                pytest.fail("parent fixture did not reach the dispatch boundary: " + detail)
            time.sleep(0.02)
        child = json.loads(ready.read_text())
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while (
            not process_has_exited(child["pid"], child["identity"]) and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert process_has_exited(child["pid"], child["identity"])
        assert run_once(config, core, counter) == ("DISCARDED" if operation == "asr" else "RETRY")
        with NodeState(config.state_directory) as state:
            resource = state.resource()
            assert resource["state"] == ("READY" if operation == "asr" else "QUARANTINED")
            if operation == "tts":
                assert state.active_job()["phase"] == "UNKNOWN"
                state.recover_manually(quarantine_id=resource["quarantine_id"],
                    operator="synthetic-operator", service_instance="synthetic-stopped-instance",
                    confirmation="inflight-execution-stopped")
            else:
                assert state.active_job() is None
        if operation == "tts":
            assert run_once(config, core, counter) == "DISCARDED"
            with NodeState(config.state_directory) as state:
                assert state.active_job() is None
        assert len(child_receipts(counter)) == 1 and core.result_bodies == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    if sys.argv[1] == "--child-fixture":
        raise SystemExit(child_entry(int(sys.argv[2]), runner=fixture_provider))
    if sys.argv[1] == "--parent-fixture":
        asyncio.run(parent_fixture(sys.argv[2]))
