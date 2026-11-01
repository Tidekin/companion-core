"""Resident local reference-voice synthesis; media remains in request memory."""

import argparse
import asyncio
import hashlib
import math
import sys
import wave
from array import array
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from companion.voice_protocol import digest
from companion_voice_node.providers import (
    GPTSoVITSConfig,
    GPTSoVITSProvider,
    LocalVoiceError,
    WAVLimits,
    validate_wav,
)
from companion_voice_node.resources import file_digest


class SpeechServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    model: str = Field(pattern=r"^[a-zA-Z0-9._:-]{1,128}$")
    voice: str = Field(default="bigfish", pattern=r"^[a-zA-Z0-9._:-]{1,128}$")
    base: GPTSoVITSConfig
    gpt_weights: Path = Field(repr=False)
    sovits_weights: Path = Field(repr=False)
    gpt_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    gpt_source: Path = Field(repr=False)
    rms_dbfs: float = Field(default=-22, ge=-36, le=-12, allow_inf_nan=False)
    timeout_seconds: float = Field(default=90, gt=0, le=180, allow_inf_nan=False)
    output: WAVLimits = Field(default_factory=WAVLimits)

    @field_validator("gpt_weights", "sovits_weights", "gpt_source")
    @classmethod
    def absolute_resource(cls, value):
        if not value.is_absolute():
            raise ValueError("Service resources must be absolute operator-configured paths")
        return value


def load_service_config(path):
    return SpeechServiceConfig.model_validate_json(Path(path).read_bytes())


def service_manifest(config):
    root = config.gpt_source
    resources = {
        "gpt_weights": config.gpt_weights, "sovits_weights": config.sovits_weights,
        "reference": config.base.reference_audio_path,
        "api_code": root / "api_v2.py",
        "inference_code": root / "GPT_SoVITS/TTS_infer_pack/TTS.py",
        "decoder_code": root / "GPT_SoVITS/module/models.py",
        "launcher_code": Path(__file__).with_name("gpt_server.py"),
        "service_code": Path(__file__),
        "provider_code": Path(__file__).with_name("providers.py"),
    }
    options = config.model_dump(mode="json", exclude={
        "base", "gpt_source", "gpt_weights", "sovits_weights",
    })
    options["base"] = config.base.model_dump(mode="json", exclude={
        "reference_audio_path", "prompt_text", "api_key",
    })
    options["base"]["prompt_text_sha256"] = hashlib.sha256(
        config.base.prompt_text.encode()
    ).hexdigest()
    return {"options": options, "resources": {
        name: file_digest(path) for name, path in resources.items()
    }}


class LocalSpeechEngine:
    def __init__(self, config):
        self.config = config
        self.base = GPTSoVITSProvider(config.base)

    async def synthesize(self, text):
        audio = await self.base.synthesize(text)
        return await asyncio.to_thread(normalize_speech, audio, self.config)


def normalize_speech(audio, config):
    """Uniform listening gain, matching the approved audition without altering timing."""
    audio = validate_wav(audio.audio, config.output)
    with wave.open(BytesIO(audio.audio), "rb") as reader:
        samples = array("h", reader.readframes(reader.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max(abs(sample) for sample in samples)
    if not peak:
        raise LocalVoiceError("VOICE_TTS_OUTPUT_INVALID")
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    gain = min(32768 * 10 ** (config.rms_dbfs / 20) / rms, 32768 * 0.891 / peak)
    samples = array("h", (round(sample * gain) for sample in samples))
    if sys.byteorder != "little":
        samples.byteswap()
    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams((audio.channels, 2, audio.sample_rate, 0, "NONE", "not compressed"))
        writer.writeframes(samples.tobytes())
    return validate_wav(output.getvalue(), config.output)


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    model: str
    input: str = Field(min_length=1, max_length=4000)
    voice: str
    response_format: str = "wav"
    speed: float = 1.0


def _error(code, status):
    return JSONResponse({"error": {"code": code, "type": "local_speech_error",
                                   "message": "Local speech request failed"}}, status,
                        headers={"Cache-Control": "no-store"})


async def _speech_request(request):
    if (request.headers.get("content-type", "").split(";")[0].strip().lower()
            != "application/json" or request.headers.get("content-encoding", "identity")
            .strip().lower() != "identity"):
        raise ValueError
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 16384:
            raise ValueError
        data.extend(chunk)
    return SpeechRequest.model_validate_json(data)


def create_app(config, *, engine=None):
    engine = engine or LocalSpeechEngine(config)

    @asynccontextmanager
    async def lifespan(app):
        app.state.ready = False
        app.state.quarantined = False
        app.state.pending = None
        app.state.manifest = digest(await asyncio.to_thread(service_manifest, config))
        # Warm the pinned native model before readiness, outside any owner job.
        await engine.synthesize("语音服务已就绪。")
        if digest(await asyncio.to_thread(service_manifest, config)) != app.state.manifest:
            raise RuntimeError("VOICE_MANIFEST_CHANGED")
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            if app.state.pending is not None:
                await asyncio.shield(app.state.pending)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def health():
        if not app.state.ready or app.state.quarantined:
            return _error("VOICE_TTS_UNAVAILABLE", 503)
        return JSONResponse({"status": "ready", "model": config.model,
                             "manifest_sha256": app.state.manifest},
                            headers={"Cache-Control": "no-store"})

    @app.post("/v1/audio/speech")
    async def speech(request: Request):
        try:
            body = await _speech_request(request)
        except (ValueError, ValidationError):
            return _error("VOICE_TEXT_INVALID", 400)
        if (body.model != config.model or body.voice != config.voice
                or body.response_format != "wav" or body.speed != 1.0
                or not body.input.strip() or len(body.input) > config.base.max_text_chars):
            return _error("VOICE_TEXT_INVALID", 400)
        if not app.state.ready or app.state.quarantined:
            return _error("VOICE_TTS_UNAVAILABLE", 503)
        if app.state.pending is not None:
            return _error("VOICE_BUSY", 429)

        async def synthesize():
            try:
                result = await engine.synthesize(body.input)
                return validate_wav(result.audio, config.output)
            except LocalVoiceError as error:
                if error.uncertain:
                    app.state.quarantined = True
                raise
            except Exception:
                app.state.quarantined = True
                raise LocalVoiceError("VOICE_TTS_EXECUTION_FAILED", uncertain=True) from None

        def finished(task):
            app.state.pending = None
            if not task.cancelled():
                task.exception()  # Consume a result even after HTTP cancellation/timeout.

        task = asyncio.create_task(synthesize())
        app.state.pending = task
        task.add_done_callback(finished)
        try:
            async with asyncio.timeout(config.timeout_seconds):
                audio = await asyncio.shield(task)
            return Response(audio.audio, media_type="audio/wav",
                            headers={"Cache-Control": "no-store"})
        except TimeoutError:
            return _error("VOICE_TTS_TIMEOUT", 504)
        except LocalVoiceError as error:
            return _error(error.code, 503)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--port", default=19880, type=int)
    parser.add_argument("--print-manifest", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535")
    config = load_service_config(args.config)
    if args.print_manifest:
        print(digest(service_manifest(config)))
        return
    import uvicorn

    uvicorn.run(create_app(config), host="127.0.0.1", port=args.port, access_log=False,
                limit_concurrency=4, timeout_graceful_shutdown=180)


if __name__ == "__main__":
    main()
