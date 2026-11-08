"""A TTS-only node must not strand an authenticated owner voice input."""

import base64
import json
import ssl

import httpx

from companion.core import Core
from companion.model import ModelClient
from companion.voice_config import VoiceNode
from companion.voice_jobs import INPUT_RECEIPT
from companion.voice_protocol import canonical_bytes
from tests.conftest import run
from tests.support.voice_flow import signed_input
from tests.support.voice_jobs import TOKEN, voice_config
from tests.support.voice_node_api import tls_core
from tests.voice_owner_fixtures import native_voice


def test_tts_only_node_projects_unavailable_asr_and_independent_worker_replies(core, tmp_path):
    voice = voice_config(nodes={
        "tts-only": VoiceNode(token=TOKEN, manifest_sha256="b" * 64, capabilities=("tts",)),
    })
    assert voice.asr.enabled
    capture, key, _ = native_voice(core, tmp_path, config=voice)
    event = core.store.one('SELECT * FROM events WHERE event_id=%s', (capture.event_id,))
    calls = []

    def model(request):
        calls.append(json.loads(request.content)["messages"])
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "这条语音暂时无法转写，请补充文字。"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    async def scenario():
        async with tls_core(core, tmp_path) as (origin, certificate), httpx.AsyncClient(
            base_url=origin, verify=ssl.create_default_context(cafile=str(certificate)),
            trust_env=False, headers={"Authorization": "Bearer "
                + core.settings.bridge_token.get_secret_value()}) as client, \
                httpx.AsyncClient(transport=httpx.MockTransport(model)) as model_http:
            original_body = event["body_json"]
            path = "/internal/v1/voice-inputs/" + event["event_id"]
            with core.store.read_snapshot():
                state = await client.get(path + "/state")
            assert state.status_code == 200, state.text
            assert state.json()["state"] == "FALLBACK"
            assert state.json()["records"] == [{
                "index": 0, "state": "MISSING", "job_id": None,
                "input_sha256": None, "source_proof_sha256": None,
                "upload_allowed": False,
            }]
            assert not core.store.one('SELECT 1 FROM runtime_meta WHERE key=%s',
                (INPUT_RECEIPT + event["event_id"],))
            assert "audio_base64" not in state.text and "transcript" not in state.text

            data, proof = signed_input(core, event["event_id"], key, index=0)
            rejected = await client.post(path, content=data, headers={
                "Content-Type": "audio/wav",
                "x-voice-proof": base64.urlsafe_b64encode(canonical_bytes(proof)).decode(),
            })
            assert rejected.status_code in {403, 409}, rejected.text
            assert not core.store.rows("SELECT * FROM voice_jobs")
            assert not core.store.rows("SELECT * FROM model_usage")

            with Core(core.settings, ModelClient(core.settings, model_http),
                      core.clock) as worker:
                assert worker.store is not core.store
                assert await worker.run_once("tts-only-asr-fallback")
            assert len(calls) == 1
            evidence = [json.loads(message["content"].split("\n", 1)[1])
                        for message in calls[0]
                        if isinstance(message["content"], str)
                        and '"kind":"untrusted_transcription"' in message["content"]]
            assert len(evidence) == 1
            assert evidence[0]["status"] == "UNAVAILABLE"
            assert evidence[0]["transcripts"] == []
            assert evidence[0]["unavailable"] == [{
                "segment_index": 0, "reason": "VOICE_ASR_NODE_UNAVAILABLE",
            }]
            assert evidence[0]["coverage"] == "PARTIAL"
            assert core.store.one('SELECT state FROM jobs WHERE event_id=%s',
                                   (event["event_id"],))["state"] == "SUCCEEDED"
            assert core.store.rows("SELECT scenario,state FROM model_usage") == [
                {"scenario": "chat", "state": "SETTLED"},
            ]
            assert len(core.store.rows("SELECT * FROM outbox")) == 1
            assert not core.store.rows("SELECT * FROM voice_jobs")
            assert core.store.one('SELECT body_json FROM events WHERE event_id=%s',
                                   (event["event_id"],))["body_json"] == original_body
            with core.store.read_snapshot():
                settled = await client.get(path + "/state")
            assert settled.status_code == 200, settled.text
            assert settled.json()["state"] == "FALLBACK"
            assert all(not item["upload_allowed"] for item in settled.json()["records"])
            with Core(core.settings, ModelClient(core.settings, model_http),
                      core.clock) as restarted:
                assert not await restarted.run_once("tts-only-restart")
            assert len(calls) == 1

    run(scenario())
