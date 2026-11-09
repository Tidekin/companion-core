"""Native owner API, independent Core and TLS voice worker with synthetic providers."""

import base64
import hashlib
import json
import ssl

import httpx
import pytest

from companion.core import Core
from companion.model import ModelClient
from companion.qq_rich import input_digest
from companion.voice_config import VoiceNode
from companion.voice_jobs import VoiceJobs, sign_asr_proof
from companion.voice_protocol import canonical_bytes, digest
from companion_voice_node.worker import local_manifest
from tests.conftest import run
from tests.support.voice_jobs import voice_config
from tests.support.voice_node_api import independent_node, tls_core
from tests.support.voice_node_worker import child_receipts
from tests.support.voice_node_worker import node as node  # noqa: F401
from tests.voice_owner_fixtures import native_voice
from tests.voice_service_fixtures import wav_bytes


@pytest.mark.parametrize("operation", ["tts", "asr"])
def test_owner_voice_api_and_independent_worker(core, tmp_path, node, operation):
    config, signing_key, counter = node
    config = config.model_copy(update={
        "node_id": "local", "request_timeout_seconds": 10.0,
        "heartbeat_seconds": 0.5, "execution_timeout_seconds": 20.0,
    })
    voice = voice_config(
        signing_private_key=signing_key.private_bytes_raw().hex(), reply={"mode": "auto"},
        nodes={"local": VoiceNode(token=config.credential,
                                   manifest_sha256=digest(local_manifest(config)))},
    )
    capture, key, _ = native_voice(core, tmp_path, record=operation == "asr", config=voice)
    assert core.settings.live_text_window is None
    source = core.store.one('SELECT * FROM events WHERE event_id=%s', (capture.event_id,))
    jobs = VoiceJobs(core.store, core.settings, core.clock)
    model_calls = []

    def provider(request):
        model_calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "原生授权合成语音回复。"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    async def scenario():
        async with tls_core(core, tmp_path) as (origin, certificate):
            async with httpx.AsyncClient(base_url=origin,
                    verify=ssl.create_default_context(cafile=str(certificate)),
                    trust_env=False, headers={"Authorization": "Bearer "
                        + core.settings.bridge_token.get_secret_value()}) as api:
                if operation == "asr":
                    data = wav_bytes(0.125)
                    proof = sign_asr_proof(key, authority=capture.authority,
                        event_id=capture.event_id, source_event_id=source["source_event_id"],
                        source_input_sha256=input_digest(json.loads(source["body_json"])),
                        source_segment_index=0, audio_sha256=hashlib.sha256(data).hexdigest(),
                        audio_bytes=len(data), source_partial=True)
                    response = await api.post("/internal/v1/voice-inputs/" + capture.event_id,
                        content=data, headers={"Content-Type": "audio/wav", "x-voice-proof":
                            base64.urlsafe_b64encode(canonical_bytes(proof)).decode()})
                    assert response.status_code == 200, response.text
                    job = core.store.one("SELECT * FROM voice_jobs WHERE operation='asr'")
                    assert job["input_blob"] == data
                else:
                    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model:
                        with Core(core.settings, ModelClient(core.settings, model),
                                  core.clock) as worker:
                            assert worker.store is not core.store
                            assert await worker.run_once("native-voice-reply")
                    outbox = core.store.one("SELECT * FROM outbox")
                    with core.store.transaction():
                        created = jobs.create_tts(outbox["message_id"],
                            json.loads(outbox["content_json"])["text"])
                    job = core.store.one('SELECT * FROM voice_jobs WHERE job_id=%s',
                                         (created["job_id"],))
                assert json.loads(job["authority_json"]) == capture.authority
                assert job["window_sha256"] is None
                usages = core.store.rows("SELECT request_id,state FROM model_usage")
                await independent_node(config.model_copy(update={"core_origin": origin}),
                    certificate, counter, tmp_path, core.clock.now.timestamp())
                assert jobs.status(job["job_id"])["state"] == (
                    "READY" if operation == "tts" else "TRANSCRIBED")
                result = jobs.result(job["job_id"])
                assert result.kind == operation
                if operation == "tts":
                    assert result.audio_bytes() == wav_bytes(0.125)
                    assert core.store.one("SELECT state FROM outbox")["state"] == "PENDING"
                    assert len(usages) == 1 and usages[0]["state"] == "SETTLED"
                else:
                    assert jobs.untrusted_transcription(job["job_id"])["partial"] is True
                    assert usages == []
                assert core.store.rows("SELECT request_id,state FROM model_usage") == usages
                assert core.store.one('SELECT body_json FROM events WHERE event_id=%s',
                    (capture.event_id,))["body_json"] == source["body_json"]
                assert len(child_receipts(counter)) == 1
                assert len(model_calls) == int(operation == "tts")

    run(scenario())
