import asyncio
import base64
import json
import math
import os
import socket
import struct
import subprocess
import sys
import wave
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from companion.voice_protocol import VoiceAudioResult, VoiceProtocolError, digest
from companion_voice_node.providers import (
    GPTSoVITSConfig,
    GPTSoVITSProvider,
    LocalVoiceError,
    OpenAISpeechConfig,
    OpenAISpeechProvider,
    validate_wav,
)
from companion_voice_node.speech_service import create_app, normalize_speech, service_manifest
from companion_voice_node.supervisor import ProcessSupervisor, _environment
from companion_voice_node.worker import VoiceNodeWorker
from tests.conftest import run
from tests.test_voice_node_worker import node as node  # noqa: F401
from tests.voice_service_fixtures import speech_config, wav_bytes


class Engine:
    def __init__(self):
        self.requests = []
        self.delay = 0
        self.failure = None

    async def synthesize(self, text):
        self.requests.append(text)
        await asyncio.sleep(self.delay)
        if self.failure:
            raise self.failure
        return validate_wav(wav_bytes())


def body(config, **updates):
    return {"model": config.model, "input": "你好，这是新的回复。", "voice": config.voice,
            "response_format": "wav", **updates}


def test_native_service_warms_once_and_returns_only_valid_wav(tmp_path):
    config, engine = speech_config(tmp_path), Engine()
    app = create_app(config, engine=engine)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://127.0.0.1") as client:
                health = await client.get("/healthz")
                assert health.status_code == 200
                assert health.json()["manifest_sha256"] == digest(service_manifest(config))
                for _ in range(2):
                    response = await client.post("/v1/audio/speech", json=body(config))
                    assert response.status_code == 200
                    assert response.headers["cache-control"] == "no-store"
                    assert validate_wav(response.content).duration_seconds == 1
        assert len(engine.requests) == 3  # one startup warmup, two owner requests

    run(scenario())


@pytest.mark.parametrize("updates", [
    {"model": "other"}, {"voice": "other"}, {"response_format": "mp3"}, {"speed": 2},
    {"input": " "}, {"input": "字" * 1001}, {"command": "not-allowed"},
])
def test_service_rejects_request_driven_model_or_execution_changes(tmp_path, updates):
    config, engine = speech_config(tmp_path), Engine()
    app = create_app(config, engine=engine)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://127.0.0.1") as client:
                response = await client.post("/v1/audio/speech", json=body(config, **updates))
                assert response.status_code == 400
                assert "你好" not in response.text and str(tmp_path) not in response.text
        assert len(engine.requests) == 1

    run(scenario())


def test_http_timeout_keeps_execution_slot_until_provider_really_finishes(tmp_path):
    config = speech_config(tmp_path).model_copy(update={"timeout_seconds": 0.03})
    engine = Engine()
    app = create_app(config, engine=engine)

    async def scenario():
        async with app.router.lifespan_context(app):
            engine.delay = 0.12
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://127.0.0.1") as client:
                first = await client.post("/v1/audio/speech", json=body(config))
                assert first.status_code == 504
                assert app.state.pending is not None
                second = await client.post("/v1/audio/speech", json=body(config))
                assert second.status_code == 429
                await asyncio.shield(app.state.pending)
                await asyncio.sleep(0)
                engine.delay = 0
                assert (await client.post("/v1/audio/speech", json=body(config))).status_code == 200
        assert len(engine.requests) == 3

    run(scenario())


def test_uncertain_base_inference_quarantines_resident_service(tmp_path):
    config, engine = speech_config(tmp_path), Engine()
    app = create_app(config, engine=engine)

    async def scenario():
        async with app.router.lifespan_context(app):
            engine.failure = LocalVoiceError("VOICE_TTS_TIMEOUT", uncertain=True)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://127.0.0.1") as client:
                assert (await client.post("/v1/audio/speech", json=body(config))).status_code == 503
                engine.failure = None
                assert (await client.get("/healthz")).status_code == 503
                assert (await client.post("/v1/audio/speech", json=body(config))).status_code == 503
        assert len(engine.requests) == 2

    run(scenario())


def test_openai_adapter_uses_pinned_model_and_bounded_static_wav():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=wav_bytes(), headers={"Content-Type": "audio/wav"})

    config = OpenAISpeechConfig(model="accepted-bigfish")
    result = run(OpenAISpeechProvider(config, transport=httpx.MockTransport(respond))
                 .synthesize("你好"))
    assert result.duration_seconds == 1
    assert len(requests) == 1
    assert str(requests[0].url) == "http://127.0.0.1:19880/v1/audio/speech"
    assert json.loads(requests[0].content) == {
        "model": "accepted-bigfish", "input": "你好", "voice": "bigfish", "response_format": "wav",
    }


@pytest.mark.parametrize("url", ["https://public.invalid", "http://127.0.0.1/path",
                                     "http://user:pass@localhost"])
def test_new_speech_config_retains_local_origin_restrictions(url):
    with pytest.raises(ValidationError):
        OpenAISpeechConfig(model="accepted", base_url=url)


def test_core_destination_pin_preserves_host_and_tls_sni(node):
    config, _, _ = node
    worker = VoiceNodeWorker(config.model_copy(update={"core_connect_address": "127.0.0.2"}))

    async def scenario():
        request = httpx.Request("GET", config.core_origin + "/internal/v2/voice-node/session")
        await worker._pin_core(request)
        assert request.url.host == "127.0.0.2"
        assert request.headers["Host"] == "core.invalid"
        assert request.extensions["sni_hostname"] == "core.invalid"
        with pytest.raises(VoiceProtocolError, match="VOICE_CORE_ORIGIN_CHANGED"):
            await worker._pin_core(httpx.Request("GET", "https://other.invalid/"))

    run(scenario())


def test_independent_provider_process_uses_native_local_speech_http_entry(tmp_path):
    config = speech_config(tmp_path / "speech")
    path = tmp_path / "service.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    workspace = Path(__file__).resolve().parents[1]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    service = subprocess.Popen(
        [sys.executable, "-B", "-m", "tests.speech_service_process", "--config", str(path),
         "--port", str(port)], cwd=workspace, env=_environment(workspace, tmp_path),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    async def scenario():
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            for _ in range(100):
                if service.poll() is not None:
                    raise AssertionError("Native local speech fixture exited during startup")
                try:
                    if (await client.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("Native local speech fixture did not become ready")
        supervisor = ProcessSupervisor(workspace, tmp_path / "provider")
        try:
            await supervisor.start(5)
            assert supervisor.process.pid != os.getpid()
            provider = OpenAISpeechConfig(model=config.model,
                                         base_url=f"http://127.0.0.1:{port}")
            await supervisor.dispatch({
                "operation": "tts", "provider": provider.model_dump(mode="json"),
                "input_base64": base64.b64encode("新的语音回复。".encode()).decode(),
                "source_partial": False,
            })
            result = await asyncio.wait_for(supervisor.result(), 10)
            assert result["status"] == "SUCCEEDED"
            payload = VoiceAudioResult.model_validate(result["payload"])
            assert validate_wav(base64.b64decode(payload.audio_base64)).duration_seconds == 1
            assert await supervisor.finish()
        finally:
            assert await supervisor.stop()

    try:
        run(scenario())
    finally:
        service.terminate()
        try:
            service.wait(10)
        except subprocess.TimeoutExpired:
            service.kill()
            service.wait(5)


def test_gpt_launcher_does_not_persist_native_transcript_console_output(tmp_path):
    script = tmp_path / "api_v2.py"
    script.write_text("import sys\nprint('private transcript')\n"
                      "print('private phonemes', file=sys.stderr)\n"
                      "assert sys.argv[1:3] == ['-a', '127.0.0.1']\n", encoding="utf-8")
    config = tmp_path / "inference.yaml"
    config.write_text("synthetic config", encoding="utf-8")
    result = subprocess.run([sys.executable, "-B", "-m", "companion_voice_node.gpt_server",
                             "--script", str(script), "--config", str(config), "--port", "19881"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stdout == result.stderr == ""


@pytest.mark.parametrize("spike", [False, True])
def test_listening_gain_keeps_timing_and_bounds_without_compressing_the_waveform(tmp_path, spike):
    samples = ([32767] + [0]*1599 if spike else
               [round(1000*math.sin(2*math.pi*200*i/16000)) for i in range(1600)])
    stream = BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        writer.writeframes(struct.pack("<1600h", *samples))
    original = validate_wav(stream.getvalue())
    result = normalize_speech(original, speech_config(tmp_path))
    assert (result.frames, result.sample_rate, result.channels) == (1600, 16000, 1)
    with wave.open(BytesIO(result.audio), "rb") as reader:
        output = struct.unpack("<1600h", reader.readframes(1600))
    assert max(abs(value) for value in output) <= round(32768*.891)
    assert [value == 0 for value in output] == [value == 0 for value in samples]
    if spike:
        assert abs(output[0] - 32768*.891) <= 1
    else:
        rms = math.sqrt(sum(value*value for value in output)/len(output))/32768
        assert 20*math.log10(rms) == pytest.approx(-22, abs=.01)


def test_reference_sampler_forwards_frozen_native_parameters(tmp_path):
    config = speech_config(tmp_path).base.model_copy(update={
        "temperature": .6, "top_p": .8, "seed": 42, "text_split_method": "cut5",
    })
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=wav_bytes(), headers={"content-type": "audio/wav"})

    run(GPTSoVITSProvider(config, transport=httpx.MockTransport(respond)).synthesize("你好"))
    assert {key: requests[0][key] for key in ("temperature", "top_p", "seed")} == {
        "temperature": .6, "top_p": .8, "seed": 42,
    }
    assert requests[0].get("text_split_method", "cut5") == "cut5"
    assert requests[0].get("speed_factor", 1) == 1


@pytest.mark.parametrize("values", [
    {"temperature": float("nan")}, {"top_p": 0}, {"top_k": True},
    {"seed": True}, {"seed": -2}, {"text_split_method": "arbitrary"},
])
def test_native_sampler_rejects_invalid_operator_parameters(tmp_path, values):
    base = speech_config(tmp_path).base.model_dump()
    with pytest.raises(ValidationError):
        GPTSoVITSConfig.model_validate({**base, **values})
