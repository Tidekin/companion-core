"""Shared synthetic voice jobs setup for component and process tests."""

import base64
import hashlib
import json

from companion import voice_authority
from companion.qq_rich import input_digest
from companion.voice_config import VoiceConfig, VoiceNode
from companion.voice_jobs import (
    VoiceJobs,
    sign_asr_proof,
)
from companion.voice_protocol import (
    VoiceAudioResult,
    VoiceClaimRequest,
    VoiceControlRequest,
    VoiceLeaseRef,
    VoiceTranscriptResult,
    make_voice_result,
)
from companion_voice_node.providers import validate_wav
from tests.voice_service_fixtures import wav_bytes

TOKEN = "independent-voice-node-credential-" + "x" * 32


TEXT = "这是一条待发送的回复。"


def voice_config(**updates):
    return VoiceConfig(
        **{
            "enabled": True,
            "signing_private_key": "61" * 32,
            "nodes": {"local": VoiceNode(token=TOKEN, manifest_sha256="b" * 64)},
            "reply": {"mode": "auto"},
        }
        | updates
    )


def prepared(core, monkeypatch, *, make_reply=True, record_count=1,
             expires_seconds=None, **updates):
    from pathlib import Path

    from tests.voice_owner_fixtures import native_reply, native_voice

    capture, capture_key, _ = native_voice(
        core, Path(core.settings.database).parent, config=voice_config(**updates),
        record=bool(record_count), record_count=record_count, expires_seconds=expires_seconds,
    )
    message_id = native_reply(core, TEXT) if make_reply else None
    return (VoiceJobs(core.store, core.settings, core.clock),
            message_id, capture.event_id, capture_key)


def create_tts(core, jobs, message_id):
    with core.store.transaction():
        return jobs.create_tts(message_id, TEXT)


def create_asr(core, jobs, event_id, key, *, data=None, partial=False, proof_change=None):
    data = data if data is not None else wav_bytes(0.125)
    event = core.store.one("SELECT * FROM events WHERE event_id=?", (event_id,))
    proof = sign_asr_proof(
        key,
        authority=voice_authority.current(
            core.store, core.settings, core.clock).authority.as_dict(),
        event_id=event_id,
        source_event_id=event["source_event_id"],
        source_input_sha256=input_digest(json.loads(event["body_json"])),
        source_segment_index=0,
        audio_sha256=hashlib.sha256(data).hexdigest(),
        audio_bytes=len(data),
        source_partial=partial,
    )
    if proof_change:
        proof = proof.model_copy(update=proof_change)
    with core.store.transaction():
        return jobs.create_asr(
            event_id, data, source_segment_index=0, upload_proof=proof, source_partial=partial
        )


def claim(jobs, **updates):
    session = jobs.session("local")
    request = VoiceClaimRequest(
        **{
            "node_id": "local",
            "incarnation": session.incarnation,
            "sequence": session.last_sequence + 1,
            "manifest_sha256": "b" * 64,
            "resource_state": "READY",
        }
        | updates
    )
    return request, jobs.claim("local", request)


def control(jobs, ref, phase="RUNNING", resource="RUNNING", **extra):
    return jobs.control(
        "local", VoiceControlRequest(ref=ref, phase=phase, resource_state=resource, **extra)
    )


def result(ref, *, operation="tts", partial=False, seconds=0.125, text="测试结果"):
    if operation == "tts":
        audio = validate_wav(wav_bytes(seconds))
        payload = VoiceAudioResult(
            audio_base64=base64.b64encode(audio.audio).decode(),
            audio_sha256=audio.sha256,
            sample_rate=audio.sample_rate,
            channels=1,
            frames=audio.frames,
            duration_seconds=audio.duration_seconds,
        )
    else:
        payload = VoiceTranscriptResult(
            text=text,
            segments=({"start_seconds": 0.0, "end_seconds": seconds, "text": text},),
            language="zh",
            language_probability=0.9,
            duration_seconds=seconds,
            partial=partial,
        )
    return make_voice_result(ref, payload)


def start(core, jobs, message_id):
    job = create_tts(core, jobs, message_id)
    request, response = claim(jobs)
    ref = VoiceLeaseRef.from_lease(response.lease)
    assert jobs.input("local", ref) == (TEXT.encode(), "text/plain")
    return job, request, response, ref
