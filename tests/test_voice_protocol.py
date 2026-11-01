import base64
import hashlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from companion.voice_protocol import (
    VoiceAudioResult,
    VoiceControlResponse,
    VoiceLease,
    VoiceLeaseRef,
    VoiceProtocolError,
    VoiceResult,
    VoiceTranscriptResult,
    canonical_bytes,
    digest,
    make_voice_result,
    sign_voice_lease,
    verify_voice_lease,
)
from tests.test_local_voice_providers import wav_bytes


def test_delivery_contracts_import_without_runtime_or_bridge_packages(tmp_path):
    root = Path(__file__).resolve().parents[1]
    package = tmp_path / "companion"
    package.mkdir()
    for name in ("__init__.py", "voice_protocol.py"):
        (package / name).write_bytes((root / "companion" / name).read_bytes())
    shared = tmp_path / "companion_delivery_protocol"
    shared.mkdir()
    for name in ("__init__.py", "authority.py", "singing.py"):
        (shared / name).write_bytes((root / "companion_delivery_protocol" / name).read_bytes())
    source = """
import importlib.abc, sys
sys.path.insert(0, sys.argv[1])
class NoRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'companion.voice_flow', 'companion.voice_jobs', 'companion.live_text',
                        'companion.core', 'companion.store', 'companion.private_life',
                        'companion_onebot_bridge', 'companion_voice_node'}:
            raise ImportError('RUNTIME_DEPENDENCY:' + fullname)
sys.meta_path.insert(0, NoRuntime())
from companion.voice_protocol import VoiceDeliveryBinding, VoiceDeliveryEnvelope
assert VoiceDeliveryEnvelope.model_fields['binding'].annotation is VoiceDeliveryBinding
print('standalone voice delivery contracts loaded')
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", source, str(tmp_path)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "standalone voice delivery contracts loaded"


def lease_values(**changes):
    now = datetime.now(UTC)
    return {
        "incarnation": "c" * 32,
        "claim_sequence": 1,
        "stop_token": "s" * 48,
        "privacy_epoch": 0,
        "job_id": "voice_" + "a" * 32,
        "generation": 1,
        "node_id": "local-gpu",
        "operation": "tts",
        "lease_token": "t" * 48,
        "input_sha256": "1" * 64,
        "input_bytes": 6,
        "input_mime": "text/plain",
        "source_partial": False,
        "manifest_sha256": "2" * 64,
        "issued_at": now,
        "server_time": now,
        "lease_expires_at": now + timedelta(seconds=20),
        "job_deadline_at": now + timedelta(seconds=90),
        **changes,
    }


def test_lease_signature_is_domain_bound_and_survives_json_roundtrip():
    key = Ed25519PrivateKey.generate()
    signed = sign_voice_lease(VoiceLease(**lease_values()), key)
    parsed = type(signed).model_validate_json(canonical_bytes(signed))
    verify_voice_lease(
        parsed,
        key.public_key().public_bytes_raw(),
        node_id=signed.node_id,
        manifest_sha256=signed.manifest_sha256,
    )
    assert parsed == signed
    assert signed.lease_token not in repr(signed)
    assert signed.signature not in repr(signed)
    assert VoiceLeaseRef.from_lease(signed).input_sha256 == signed.input_sha256


@pytest.mark.parametrize(
    "change",
    [
        {"generation": 2},
        {"input_sha256": "3" * 64},
        {"input_bytes": 7},
        {"lease_token": "new" * 16},
        {"source_partial": True},
    ],
)
def test_signed_lease_rejects_tampering(change):
    key = Ed25519PrivateKey.generate()
    signed = sign_voice_lease(VoiceLease(**lease_values()), key).model_copy(update=change)
    with pytest.raises(VoiceProtocolError, match="VOICE_LEASE_SIGNATURE_INVALID"):
        verify_voice_lease(
            signed,
            key.public_key().public_bytes_raw(),
            node_id="local-gpu",
            manifest_sha256="2" * 64,
        )


@pytest.mark.parametrize(("node", "manifest"), [("other", "2" * 64), ("local-gpu", "3" * 64)])
def test_signature_does_not_authorize_another_node_or_configuration(node, manifest):
    key = Ed25519PrivateKey.generate()
    signed = sign_voice_lease(VoiceLease(**lease_values()), key)
    with pytest.raises(VoiceProtocolError, match="VOICE_LEASE_SCOPE_MISMATCH"):
        verify_voice_lease(
            signed, key.public_key().public_bytes_raw(), node_id=node, manifest_sha256=manifest
        )


@pytest.mark.parametrize(
    "change",
    [
        {"generation": True},
        {"generation": "1"},
        {"schema_version": True},
        {"input_bytes": 0},
        {"input_bytes": 16001},
        {"source_partial": 1},
        {"source_partial": True},
        {"input_mime": "audio/wav"},
        {"extra": "not permitted"},
        {"job_id": "../../another-route"},
        {"lease_token": "short"},
    ],
)
def test_lease_has_strict_shape_and_operation_scope(change):
    with pytest.raises(ValidationError):
        VoiceLease(**lease_values(**change))


def test_lease_requires_utc_ordered_bounded_times():
    now = datetime.now(UTC)
    cases = [
        {"issued_at": now.replace(tzinfo=None)},
        {"lease_expires_at": now - timedelta(seconds=1)},
        {"job_deadline_at": now + timedelta(seconds=700)},
        {
            "lease_expires_at": now + timedelta(seconds=121),
            "job_deadline_at": now + timedelta(seconds=150),
        },
    ]
    for values in cases:
        with pytest.raises(ValidationError):
            VoiceLease(**lease_values(**values))


@pytest.mark.parametrize(
    "body",
    [
        {"action": "CONTINUE"},
        {"action": "COMMITTED"},
        {"action": "CANCEL", "committed_result_sha256": "a" * 64},
    ],
)
def test_control_requires_the_payload_for_its_action(body):
    with pytest.raises(ValidationError):
        VoiceControlResponse(**body)


def transcript(**changes):
    return {
        "text": "你好世界",
        "segments": [
            {"start_seconds": 0.0, "end_seconds": 0.5, "text": "你好"},
            {"start_seconds": 0.6, "end_seconds": 1.0, "text": "世界"},
        ],
        "language": "zh",
        "language_probability": 0.9,
        "duration_seconds": 1.0,
        "partial": True,
        **changes,
    }


def test_result_digest_binds_payload_and_generation_reference():
    lease = VoiceLease(**lease_values(operation="asr", input_mime="audio/wav", source_partial=True))
    payload = VoiceTranscriptResult(**transcript())
    result = make_voice_result(VoiceLeaseRef.from_lease(lease), payload)
    assert result.result_sha256 == digest(payload)
    assert VoiceResult.model_validate_json(canonical_bytes(result)) == result
    altered = result.model_dump(mode="json")
    altered["payload"]["partial"] = False
    with pytest.raises(ValidationError):
        VoiceResult(**altered)
    assert "你好" not in repr(result)


@pytest.mark.parametrize(
    "change",
    [
        {"text": "not the segments"},
        {"partial": "true"},
        {"language_probability": float("nan")},
        {"segments": [{"start_seconds": -0.1, "end_seconds": 1.0, "text": "你好世界"}]},
        {"segments": [{"start_seconds": 0.1, "end_seconds": 2.0, "text": "你好世界"}]},
        {"segments": [{"start_seconds": True, "end_seconds": 1.0, "text": "你好世界"}]},
    ],
)
def test_transcript_rejects_fabricated_or_unbounded_evidence(change):
    with pytest.raises(ValidationError):
        VoiceTranscriptResult(**transcript(**change))


def test_audio_payload_limits_decoding_and_matches_its_digest():
    data = wav_bytes(0.1)
    values = {
        "audio_base64": base64.b64encode(data).decode(),
        "audio_sha256": hashlib.sha256(data).hexdigest(),
        "sample_rate": 16000,
        "channels": 1,
        "sample_width": 2,
        "frames": 1600,
        "duration_seconds": 0.1,
    }
    assert VoiceAudioResult(**values).audio_bytes() == data
    for change in (
        {"audio_sha256": "0" * 64},
        {"audio_base64": "!" * 80},
        {"frames": 1},
        {"channels": True},
        {"sample_width": True},
        {"duration_seconds": float("inf")},
    ):
        with pytest.raises(ValidationError):
            VoiceAudioResult(**(values | change))


def test_audio_result_checks_actual_wav_even_when_payload_hash_is_correct():
    data = bytearray(wav_bytes(0.1))
    data[34:36] = (12).to_bytes(2, "little")
    with pytest.raises(ValidationError):
        VoiceAudioResult(
            audio_base64=base64.b64encode(data).decode(),
            audio_sha256=hashlib.sha256(data).hexdigest(),
            sample_rate=16000,
            channels=1,
            frames=1600,
            duration_seconds=0.1,
        )
