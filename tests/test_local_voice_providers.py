import asyncio
import hashlib
import json
import struct
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from companion_voice_node import (
    FasterWhisperConfig,
    FasterWhisperProvider,
    GPTSoVITSConfig,
    GPTSoVITSProvider,
    LocalVoiceError,
    LocalVoiceUnavailable,
    WAVLimits,
    validate_wav,
)
from tests.voice_service_fixtures import wav_bytes


@pytest.fixture
def tts_config(tmp_path):
    reference = tmp_path / "operator-reference.wav"
    reference.write_bytes(wav_bytes(3))
    return GPTSoVITSConfig(reference_audio_path=reference, prompt_text="参考音频原文")


@pytest.fixture
def asr_config(tmp_path):
    model = tmp_path / "local-model"
    model.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (model / name).write_bytes(b"local stub for factory injection")
    return FasterWhisperConfig(model_dir=model)


def segment(start=0, end=1, text="你好"):
    return SimpleNamespace(start=start, end=end, text=text)


def model_factory(segments, *, language="zh", probability=0.95, calls=None):
    def factory(path, **kwargs):
        if calls is not None:
            calls.append((path, kwargs))

        class Model:
            def transcribe(self, audio, **options):
                if calls is not None:
                    calls.append((audio.read(), options))
                return iter(segments), SimpleNamespace(
                    language=language,
                    language_probability=probability,
                )

        return Model()

    return factory


def test_validated_audio_has_measured_metadata_and_hash():
    data = wav_bytes(0.25, rate=24000)
    audio = validate_wav(data)
    assert (audio.frames, audio.sample_rate, audio.channels, audio.sample_width) == (
        6000,
        24000,
        1,
        2,
    )
    assert audio.duration_seconds == 0.25
    assert audio.mime_type == "audio/wav"
    assert audio.audio == data and audio.sha256 == hashlib.sha256(data).hexdigest()
    assert "audio=" not in repr(audio)


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", "VOICE_INVALID_WAV"),
        (b"not audio", "VOICE_INVALID_WAV"),
        (wav_bytes()[:-2], "VOICE_INVALID_WAV"),
        (wav_bytes() + b"trailing bytes", "VOICE_INVALID_WAV"),
        (wav_bytes(0), "VOICE_INVALID_WAV"),
        (wav_bytes(rate=8000), "VOICE_WAV_FORMAT_UNSUPPORTED"),
        (wav_bytes(channels=2), "VOICE_WAV_FORMAT_UNSUPPORTED"),
        (wav_bytes(width=1), "VOICE_WAV_FORMAT_UNSUPPORTED"),
    ],
    ids=["empty", "not-wav", "truncated", "extra-bytes", "no-frames", "rate", "stereo", "width"],
)
def test_wav_rejects_empty_truncated_or_unsupported_audio(data, code):
    with pytest.raises(LocalVoiceError, match=f"^{code}$"):
        validate_wav(data)


def test_wav_rejects_partial_frame_even_when_riff_length_is_consistent():
    data = bytearray(wav_bytes())
    data[40:44] = struct.pack("<I", len(data) - 45)
    with pytest.raises(LocalVoiceError, match="VOICE_INVALID_WAV"):
        validate_wav(bytes(data))


@pytest.mark.parametrize(
    ("offset", "replacement", "code"),
    [
        (34, struct.pack("<H", 12), "VOICE_WAV_FORMAT_UNSUPPORTED"),
        (32, struct.pack("<H", 4), "VOICE_INVALID_WAV"),
        (28, struct.pack("<I", 16000), "VOICE_INVALID_WAV"),
    ],
    ids=["12-bit-pcm", "wrong-block-alignment", "wrong-byte-rate"],
)
def test_wav_validates_pcm16_header_fields_ignored_by_wave_reader(offset, replacement, code):
    data = bytearray(wav_bytes())
    data[offset : offset + len(replacement)] = replacement
    with pytest.raises(LocalVoiceError, match=f"^{code}$"):
        validate_wav(bytes(data))


def test_wav_enforces_independent_byte_and_duration_limits():
    data = wav_bytes(1)
    with pytest.raises(LocalVoiceError, match="VOICE_AUDIO_TOO_LARGE"):
        validate_wav(data, WAVLimits(max_bytes=100))
    with pytest.raises(LocalVoiceError, match="VOICE_AUDIO_TOO_LONG"):
        validate_wav(data, WAVLimits(max_duration_seconds=0.5))
    assert validate_wav(wav_bytes(channels=2), WAVLimits(channels=2)).channels == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://192.168.1.2:9880",
        "file:///tts",
        "http://localhost:0",
        "http://user:secret@127.0.0.1",
        "http://127.0.0.1/api",
        "http://127.0.0.1?key=secret",
        "http://127.0.0.1#fragment",
    ],
)
def test_tts_config_rejects_nonlocal_or_ambiguous_origin(tts_config, url):
    values = tts_config.model_dump() | {"base_url": url}
    with pytest.raises(ValidationError) as error:
        GPTSoVITSConfig(**values)
    assert url not in str(error.value)


def test_configs_reject_unknown_fields_and_hide_secrets(tts_config, asr_config):
    config = GPTSoVITSConfig(**(tts_config.model_dump() | {"api_key": "private-key"}))
    assert "api_key" not in config.model_dump()
    assert "private-key" not in repr(config)
    assert str(config.reference_audio_path) not in repr(config)
    assert str(asr_config.model_dir) not in repr(asr_config)
    with pytest.raises(ValidationError):
        GPTSoVITSConfig(**(tts_config.model_dump() | {"download": True}))
    with pytest.raises(ValidationError):
        FasterWhisperConfig(model_dir="remote/model-id")


def test_tts_posts_only_static_wav_request_to_configured_local_service(tts_config):
    data, calls = wav_bytes(0.25), []
    config = GPTSoVITSConfig(**(tts_config.model_dump() | {"api_key": "test-key"}))

    def respond(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "audio/wav"}, content=data)

    audio = asyncio.run(
        GPTSoVITSProvider(config, transport=httpx.MockTransport(respond)).synthesize("你好")
    )
    assert audio.audio == data
    assert len(calls) == 1 and str(calls[0].url) == "http://127.0.0.1:9880/tts"
    assert calls[0].method == "POST" and calls[0].headers["accept-encoding"] == "identity"
    assert calls[0].headers["authorization"] == "Bearer test-key"
    payload = json.loads(calls[0].content)
    assert payload == {
        "text": "你好",
        "text_lang": "zh",
        "prompt_lang": "zh",
        "prompt_text": "参考音频原文",
        "ref_audio_path": str(tts_config.reference_audio_path.resolve()),
        "media_type": "wav",
        "streaming_mode": False,
        "batch_size": 1,
        "parallel_infer": False,
    }


@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [
        (400, {"content-type": "application/json"}, "VOICE_TTS_REJECTED"),
        (302, {"location": "https://external.invalid/tts"}, "VOICE_TTS_REJECTED"),
        (200, {"content-type": "audio/mpeg"}, "VOICE_TTS_FORMAT_UNSUPPORTED"),
        (
            200,
            {"content-type": "audio/wav", "content-encoding": "br"},
            "VOICE_TTS_ENCODING_UNSUPPORTED",
        ),
        (200, {"content-type": "audio/wav", "content-length": "-1"}, "VOICE_TTS_LENGTH_INVALID"),
        (200, {"content-type": "audio/wav", "content-length": "1"}, "VOICE_TTS_LENGTH_INVALID"),
        (200, {"content-type": "audio/wav", "content-length": "5000000"}, "VOICE_AUDIO_TOO_LARGE"),
    ],
)
def test_tts_rejects_bad_http_responses_without_leaking_body_or_following_redirect(
    tts_config,
    status,
    headers,
    code,
):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers=headers,
            stream=httpx.ByteStream(b"private-provider-error"),
        )

    with pytest.raises(LocalVoiceError, match=f"^{code}$") as error:
        asyncio.run(
            GPTSoVITSProvider(tts_config, transport=httpx.MockTransport(respond)).synthesize("hi")
        )
    assert len(calls) == 1
    assert "private-provider-error" not in str(error.value)


def test_tts_validates_wav_bytes_after_http_success(tts_config):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "audio/wav"},
            content=b"fake wav",
        )
    )
    with pytest.raises(LocalVoiceError, match="VOICE_INVALID_WAV"):
        asyncio.run(GPTSoVITSProvider(tts_config, transport=transport).synthesize("hi"))


def test_tts_bounds_chunked_body_and_closes_stream(tts_config):
    consumed, closed = [], []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(100):
                consumed.append(index)
                yield b"x" * 65536

        async def aclose(self):
            closed.append(True)

    config = tts_config.model_copy(update={"output": WAVLimits(max_bytes=100_000)})
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "audio/wav"},
            stream=Stream(),
        )
    )
    with pytest.raises(LocalVoiceError, match="VOICE_AUDIO_TOO_LARGE"):
        asyncio.run(GPTSoVITSProvider(config, transport=transport).synthesize("hi"))
    assert consumed == [0, 1] and closed == [True]


@pytest.mark.parametrize(
    ("exception", "code", "uncertain"),
    [
        (httpx.ReadTimeout, "VOICE_TTS_TIMEOUT", True),
        (httpx.ReadError, "VOICE_TTS_TRANSPORT_UNCERTAIN", True),
        (httpx.ConnectError, "VOICE_TTS_UNAVAILABLE", False),
    ],
)
def test_tts_transport_failure_is_sanitized_and_never_retried(
    tts_config, exception, code, uncertain
):
    calls = []

    def fail(request):
        calls.append(request)
        raise exception("private transport detail", request=request)

    with pytest.raises(LocalVoiceError, match=f"^{code}$") as error:
        asyncio.run(
            GPTSoVITSProvider(tts_config, transport=httpx.MockTransport(fail)).synthesize("hi")
        )
    assert error.value.uncertain is uncertain
    assert len(calls) == 1 and "private" not in str(error.value)


def test_tts_total_deadline_covers_slow_response_body(tts_config):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b"unreachable"

    config = tts_config.model_copy(update={"timeout_seconds": 0.02})
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "audio/wav"},
            stream=Stream(),
        )
    )
    with pytest.raises(LocalVoiceError, match="VOICE_TTS_TIMEOUT") as error:
        asyncio.run(GPTSoVITSProvider(config, transport=transport).synthesize("hi"))
    assert error.value.uncertain is True


def test_tts_fails_fast_when_busy_and_propagates_caller_cancellation(tts_config):
    async def scenario():
        entered = asyncio.Event()

        async def respond(request):
            entered.set()
            await asyncio.Event().wait()

        provider = GPTSoVITSProvider(tts_config, transport=httpx.MockTransport(respond))
        task = asyncio.create_task(provider.synthesize("first"))
        await entered.wait()
        with pytest.raises(LocalVoiceError, match="VOICE_BUSY"):
            await provider.synthesize("second")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


@pytest.mark.parametrize("reference_state", ["missing", "short", "long", "bad"])
def test_tts_requires_valid_operator_reference_before_transport(tts_config, reference_state):
    path = tts_config.reference_audio_path
    if reference_state == "missing":
        path.unlink()
    else:
        path.write_bytes(
            {"short": wav_bytes(2), "long": wav_bytes(11), "bad": b"not wav"}[reference_state]
        )
    calls = []
    provider = GPTSoVITSProvider(
        tts_config, transport=httpx.MockTransport(lambda r: calls.append(r))
    )
    with pytest.raises(LocalVoiceUnavailable):
        asyncio.run(provider.synthesize("hello"))
    assert not calls


def test_asr_consumes_all_segments_retains_boundaries_and_partial_provenance(asr_config):
    calls, consumed = [], []

    def stream():
        for value in [segment(0.1, 0.4, "你好"), segment(0.6, 1, "世界")]:
            consumed.append(value)
            yield value

    provider = FasterWhisperProvider(asr_config, model_factory=model_factory(stream(), calls=calls))
    data = wav_bytes()
    result = provider.transcribe(data, source_partial=True)
    assert result.text == "你好世界" and result.partial is True
    assert result.language == "zh" and result.language_probability == 0.95
    assert result.duration_seconds == 1 and len(consumed) == 2
    assert [(s.start_seconds, s.end_seconds) for s in result.segments] == [(0.1, 0.4), (0.6, 1)]
    assert calls[0] == (
        str(asr_config.model_dir.resolve()),
        {
            "device": "cuda",
            "device_index": 0,
            "compute_type": "int8_float16",
            "cpu_threads": 4,
            "num_workers": 1,
            "local_files_only": True,
        },
    )
    assert calls[1][0] == data
    assert calls[1][1]["condition_on_previous_text"] is False
    assert calls[1][1]["beam_size"] == 1 and calls[1][1]["vad_filter"] is True


def test_asr_silence_returns_no_fabricated_transcript_and_model_is_cached(asr_config):
    calls = []
    provider = FasterWhisperProvider(asr_config, model_factory=model_factory([], calls=calls))
    for _ in range(2):
        result = provider.transcribe(wav_bytes())
        assert result.text == "" and result.segments == () and result.partial is False
    assert len(calls) == 3  # One model load and two transcriptions.


@pytest.mark.parametrize("duration,end", [(5.6, 5.62), (5.56, 5.62), (5.6, 5.7)])
def test_asr_clamps_final_quantized_timestamp_to_actual_pcm_end(asr_config, duration, end):
    provider = FasterWhisperProvider(
        asr_config, model_factory=model_factory([segment(0.05, end, "sample")])
    )
    result = provider.transcribe(wav_bytes(duration), source_partial=True)
    assert result.duration_seconds == duration and result.partial is True
    assert result.segments[0].start_seconds == 0.05
    assert result.segments[0].end_seconds == duration


@pytest.mark.parametrize("missing", ["model.bin", "config.json", "tokenizer.json"])
def test_asr_missing_local_files_never_imports_or_downloads(asr_config, monkeypatch, missing):
    (asr_config.model_dir / missing).unlink()
    imports = []
    monkeypatch.setattr("companion_voice_node.providers.importlib.import_module", imports.append)
    with pytest.raises(LocalVoiceUnavailable, match="VOICE_ASR_MODEL_UNAVAILABLE"):
        FasterWhisperProvider(asr_config).transcribe(wav_bytes())
    assert imports == []


def test_asr_missing_dependency_is_unavailable_and_import_is_lazy(asr_config, monkeypatch):
    imports = []

    def missing(name):
        imports.append(name)
        raise ImportError("private installation path")

    monkeypatch.setattr("companion_voice_node.providers.importlib.import_module", missing)
    provider = FasterWhisperProvider(asr_config)
    assert imports == []
    with pytest.raises(LocalVoiceUnavailable, match="VOICE_ASR_DEPENDENCY_UNAVAILABLE"):
        provider.transcribe(wav_bytes())
    assert imports == ["faster_whisper"]


@pytest.mark.parametrize(
    "segments",
    [
        [segment(-0.1, 0.5)],
        [segment(0.5, 0.4)],
        [segment(0.1, 0.1)],
        [segment(0, 1.101)],
        [segment(1, 1.02)],
        [segment(1.01, 1.02)],
        [segment(float("nan"), 0.5)],
        [segment(0, float("inf"))],
        [segment(True, 1)],
        [segment(0, 0.7), segment(0.6, 1)],
        [segment(text=" ")],
        [segment(text=123)],
    ],
)
def test_asr_rejects_invalid_segment_evidence(asr_config, segments):
    provider = FasterWhisperProvider(asr_config, model_factory=model_factory(segments))
    with pytest.raises(LocalVoiceError, match="VOICE_ASR_RESULT_INVALID"):
        provider.transcribe(wav_bytes())


@pytest.mark.parametrize(
    ("language", "probability"),
    [
        ("", 0.8),
        ("invalid-language", 0.8),
        ("zh", float("nan")),
        ("zh", 1.1),
        ("zh", True),
    ],
)
def test_asr_validates_language_evidence(asr_config, language, probability):
    provider = FasterWhisperProvider(
        asr_config,
        model_factory=model_factory([], language=language, probability=probability),
    )
    with pytest.raises(LocalVoiceError, match="VOICE_ASR_RESULT_INVALID"):
        provider.transcribe(wav_bytes())


@pytest.mark.parametrize("limits", [{"max_segments": 1}, {"max_text_chars": 2}])
def test_asr_result_limits_fail_instead_of_returning_silently_truncated_success(asr_config, limits):
    closed = []

    def stream():
        try:
            yield segment(0, 0.4, "你好")
            yield segment(0.5, 1, "世界")
        finally:
            closed.append(True)

    config = asr_config.model_copy(update=limits)
    provider = FasterWhisperProvider(config, model_factory=model_factory(stream()))
    with pytest.raises(LocalVoiceError, match="VOICE_ASR_RESULT_TOO_LARGE"):
        provider.transcribe(wav_bytes())
    assert closed == [True]


def test_asr_generator_failure_does_not_return_partial_success(asr_config):
    def stream():
        yield segment(0, 0.4)
        raise RuntimeError("private model path")

    provider = FasterWhisperProvider(asr_config, model_factory=model_factory(stream()))
    with pytest.raises(LocalVoiceError, match="^VOICE_ASR_FAILED$"):
        provider.transcribe(wav_bytes())


def test_asr_deadline_is_checked_after_native_generator_completion(asr_config, monkeypatch):
    now, closed = [0.0], []
    monkeypatch.setattr("companion_voice_node.providers.time.monotonic", lambda: now[0])

    def stream():
        try:
            yield segment(0, 0.4)
            now[0] = asr_config.timeout_seconds + 1
        finally:
            closed.append(True)

    provider = FasterWhisperProvider(asr_config, model_factory=model_factory(stream()))
    with pytest.raises(LocalVoiceError, match="VOICE_ASR_TIMEOUT"):
        provider.transcribe(wav_bytes())
    assert closed == [True]


def test_asr_concurrency_rejects_reentrant_inference_before_loading_again(asr_config):
    def stream():
        with pytest.raises(LocalVoiceError, match="VOICE_BUSY"):
            provider.transcribe(wav_bytes())
        yield segment()

    provider = FasterWhisperProvider(asr_config, model_factory=model_factory(stream()))
    assert provider.transcribe(wav_bytes()).text == "你好"


def test_asr_bad_input_is_rejected_before_model_load(asr_config):
    calls = []
    provider = FasterWhisperProvider(asr_config, model_factory=model_factory([], calls=calls))
    with pytest.raises(LocalVoiceError, match="VOICE_INVALID_WAV"):
        provider.transcribe(b"mp3 is not a decoded WAV")
    with pytest.raises(LocalVoiceError, match="VOICE_ASR_PARTIAL_INVALID"):
        provider.transcribe(wav_bytes(), source_partial="yes")
    assert not calls
