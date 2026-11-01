"""Bounded local TTS/ASR adapters; no persistence, authorization, retries, or delivery.

One provider instance owns one in-process concurrency gate. The worker owns shared GPU
admission and process supervision. A TTS timeout/disconnect (or caller cancellation)
does not prove the server stopped inference: quarantine that resource until reconciled.
ASR deadlines are checked between native calls and cannot interrupt blocked CUDA code.
"""

import asyncio
import hashlib
import importlib
import ipaddress
import math
import re
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class LocalVoiceError(Exception):
    """Stable public error; never includes provider bodies, keys, or private paths."""

    def __init__(self, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


class LocalVoiceUnavailable(LocalVoiceError):
    """The configured local dependency is unavailable; no download is attempted."""


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class WAVLimits(_Config):
    max_bytes: int = Field(default=4_000_000, ge=44, le=32_000_000, strict=True)
    max_duration_seconds: float = Field(default=60, gt=0, le=600, allow_inf_nan=False)
    sample_rates: tuple[int, ...] = (16000, 22050, 24000, 32000, 44100, 48000)
    channels: Literal[1, 2] = 1

    @field_validator("sample_rates")
    @classmethod
    def valid_sample_rates(cls, value):
        if not value or len(set(value)) != len(value) or any(v < 8000 or v > 96000 for v in value):
            raise ValueError("sample_rates must contain unique supported PCM rates")
        return value


@dataclass(frozen=True)
class ValidatedAudio:
    audio: bytes = field(repr=False)
    sample_rate: int
    channels: int
    sample_width: int
    frames: int
    duration_seconds: float
    sha256: str
    mime_type: str = "audio/wav"


def validate_wav(data: bytes, limits: WAVLimits | None = None) -> ValidatedAudio:
    """Validate complete little-endian PCM16 WAV, including chunk and frame lengths."""
    limits = limits or WAVLimits()
    if not isinstance(data, bytes) or not data:
        raise LocalVoiceError("VOICE_INVALID_WAV")
    if len(data) > limits.max_bytes:
        raise LocalVoiceError("VOICE_AUDIO_TOO_LARGE")
    if (
        len(data) < 44
        or data[:4] != b"RIFF"
        or data[8:12] != b"WAVE"
        or int.from_bytes(data[4:8], "little") != len(data) - 8
    ):
        raise LocalVoiceError("VOICE_INVALID_WAV")
    offset, data_size, fmt_header = 12, None, None
    while offset < len(data):
        if offset + 8 > len(data):
            raise LocalVoiceError("VOICE_INVALID_WAV")
        kind = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        offset += 8
        if offset + size + (size % 2) > len(data):
            raise LocalVoiceError("VOICE_INVALID_WAV")
        if kind == b"fmt ":
            if fmt_header is not None or size < 16:
                raise LocalVoiceError("VOICE_INVALID_WAV")
            fmt_header = data[offset : offset + 16]
        elif kind == b"data":
            if data_size is not None or fmt_header is None:
                raise LocalVoiceError("VOICE_INVALID_WAV")
            data_size = size
        offset += size + (size % 2)
    if fmt_header is None:
        raise LocalVoiceError("VOICE_INVALID_WAV")
    if (
        int.from_bytes(fmt_header[:2], "little") != 1
        or int.from_bytes(fmt_header[14:16], "little") != 16
    ):
        raise LocalVoiceError("VOICE_WAV_FORMAT_UNSUPPORTED")
    try:
        with wave.open(BytesIO(data), "rb") as wav:
            rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
            frames = wav.getnframes()
            if (
                rate not in limits.sample_rates
                or channels != limits.channels
                or width != 2
                or wav.getcomptype() != "NONE"
            ):
                raise LocalVoiceError("VOICE_WAV_FORMAT_UNSUPPORTED")
            if frames <= 0 or data_size != frames * channels * width:
                raise LocalVoiceError("VOICE_INVALID_WAV")
            if (
                int.from_bytes(fmt_header[12:14], "little") != channels * width
                or int.from_bytes(fmt_header[8:12], "little") != rate * channels * width
            ):
                raise LocalVoiceError("VOICE_INVALID_WAV")
            duration = frames / rate
            if duration > limits.max_duration_seconds:
                raise LocalVoiceError("VOICE_AUDIO_TOO_LONG")
            if len(wav.readframes(frames + 1)) != data_size:
                raise LocalVoiceError("VOICE_INVALID_WAV")
    except (wave.Error, EOFError, ValueError, OverflowError):
        raise LocalVoiceError("VOICE_INVALID_WAV") from None
    return ValidatedAudio(
        data, rate, channels, width, frames, duration, hashlib.sha256(data).hexdigest()
    )


class GPTSoVITSConfig(_Config):
    base_url: str = "http://127.0.0.1:9880"
    reference_audio_path: Path = Field(repr=False)
    prompt_text: str = Field(min_length=1, max_length=2000, repr=False)
    prompt_lang: str = "zh"
    text_lang: str = "zh"
    media_type: Literal["wav"] = "wav"
    timeout_seconds: float = Field(default=90, gt=0, le=600, allow_inf_nan=False)
    max_concurrency: int = Field(default=1, ge=1, le=4, strict=True)
    max_text_chars: int = Field(default=1000, ge=1, le=4000, strict=True)
    parallel_infer: bool = Field(default=False, strict=True)
    speed_factor: float = Field(default=1.0, ge=0.5, le=1.5, allow_inf_nan=False)
    output: WAVLimits = Field(default_factory=WAVLimits)
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)

    @field_validator("base_url")
    @classmethod
    def loopback_url(cls, value):
        try:
            parts = urlsplit(value)
            host, port = parts.hostname, parts.port
            local = host == "localhost" or (
                host is not None and ipaddress.ip_address(host).is_loopback
            )
        except ValueError:
            raise ValueError("base_url must be a loopback HTTP origin") from None
        if (
            not local
            or parts.scheme not in {"http", "https"}
            or parts.username is not None
            or parts.password is not None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or (port is not None and port < 1)
        ):
            raise ValueError("base_url must be a loopback HTTP origin")
        return value.rstrip("/")

    @field_validator("reference_audio_path")
    @classmethod
    def absolute_reference(cls, value):
        if not value.is_absolute():
            raise ValueError("reference_audio_path must be absolute")
        return value

    @field_validator("prompt_text")
    @classmethod
    def reference_transcript(cls, value):
        if not value.strip():
            raise ValueError("prompt_text must describe the configured reference audio")
        return value.strip()

    @field_validator("prompt_lang", "text_lang")
    @classmethod
    def valid_language(cls, value):
        if not re.fullmatch(r"[a-z_]{2,16}", value):
            raise ValueError("language must be a provider language code")
        return value


def _reference_audio(config: GPTSoVITSConfig) -> str:
    # This is the operator-configured reference only, never an input-media fallback.
    try:
        path = config.reference_audio_path.resolve(strict=True)
        if not path.is_file():
            raise OSError
        with path.open("rb") as handle:
            data = handle.read(2_000_001)
    except (OSError, RuntimeError):
        raise LocalVoiceUnavailable("VOICE_REFERENCE_UNAVAILABLE") from None
    try:
        audio = validate_wav(data, WAVLimits(max_bytes=2_000_000, max_duration_seconds=10))
        if audio.duration_seconds < 3:
            raise LocalVoiceError("VOICE_REFERENCE_INVALID")
    except LocalVoiceError:
        raise LocalVoiceUnavailable("VOICE_REFERENCE_INVALID") from None
    return str(path)


class OpenAISpeechConfig(_Config):
    """Pinned local speech service; the node never chooses a model or voice."""

    base_url: str = "http://127.0.0.1:19880"
    model: str = Field(pattern=r"^[a-zA-Z0-9._:-]{1,128}$")
    voice: str = Field(default="bigfish", pattern=r"^[a-zA-Z0-9._:-]{1,128}$")
    timeout_seconds: float = Field(default=90, gt=0, le=600, allow_inf_nan=False)
    max_concurrency: int = Field(default=1, ge=1, le=4, strict=True)
    max_text_chars: int = Field(default=200, ge=1, le=4000, strict=True)
    output: WAVLimits = Field(default_factory=WAVLimits)
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)

    @field_validator("base_url")
    @classmethod
    def loopback_url(cls, value):
        return GPTSoVITSConfig.loopback_url(value)


class _WAVHTTPProvider:
    """Single-slot, bounded local HTTP synthesis with uncertainty preserved."""

    route = "/tts"

    def __init__(
        self, config: GPTSoVITSConfig | OpenAISpeechConfig,
        *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.config = config
        self.transport = transport
        self._slots = threading.BoundedSemaphore(config.max_concurrency)

    async def synthesize(self, text: str) -> ValidatedAudio:
        if not isinstance(text, str) or not text.strip() or len(text) > self.config.max_text_chars:
            raise LocalVoiceError("VOICE_TEXT_INVALID")
        if not self._slots.acquire(blocking=False):
            raise LocalVoiceError("VOICE_BUSY")
        try:
            headers = {"Accept": "audio/wav", "Accept-Encoding": "identity"}
            if self.config.api_key:
                headers["Authorization"] = "Bearer " + self.config.api_key.get_secret_value()
            payload = await self._payload(text)
            async with asyncio.timeout(self.config.timeout_seconds):
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=self.config.timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        "POST",
                        self.config.base_url + self.route,
                        headers=headers,
                        json=payload,
                    ) as response:
                        data = await self._read_audio(response)
            return validate_wav(data, self.config.output)
        except (TimeoutError, httpx.TimeoutException):
            raise LocalVoiceError("VOICE_TTS_TIMEOUT", uncertain=True) from None
        except httpx.ConnectError:
            raise LocalVoiceUnavailable("VOICE_TTS_UNAVAILABLE") from None
        except httpx.HTTPError:
            raise LocalVoiceError("VOICE_TTS_TRANSPORT_UNCERTAIN", uncertain=True) from None
        finally:
            self._slots.release()

    async def _read_audio(self, response: httpx.Response) -> bytes:
        if response.status_code != 200:
            raise LocalVoiceError("VOICE_TTS_REJECTED")
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if mime != "audio/wav":
            raise LocalVoiceError("VOICE_TTS_FORMAT_UNSUPPORTED")
        if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
            raise LocalVoiceError("VOICE_TTS_ENCODING_UNSUPPORTED")
        limit = self.config.output.max_bytes
        length = response.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal():
                raise LocalVoiceError("VOICE_TTS_LENGTH_INVALID")
            if len(length) > 10 or int(length) > limit:
                raise LocalVoiceError("VOICE_AUDIO_TOO_LARGE")
        data = bytearray()
        async for part in response.aiter_bytes(chunk_size=65536):
            if len(data) + len(part) > limit:
                raise LocalVoiceError("VOICE_AUDIO_TOO_LARGE")
            data.extend(part)
        if length is not None and len(data) != int(length):
            raise LocalVoiceError("VOICE_TTS_LENGTH_INVALID")
        return bytes(data)


class GPTSoVITSProvider(_WAVHTTPProvider):
    """Local base speech through GPT-SoVITS's native api_v2 contract."""

    async def _payload(self, text):
        reference = await asyncio.to_thread(_reference_audio, self.config)
        payload = {
            "text": text, "text_lang": self.config.text_lang,
            "ref_audio_path": reference, "prompt_text": self.config.prompt_text,
            "prompt_lang": self.config.prompt_lang, "media_type": self.config.media_type,
            "streaming_mode": False, "batch_size": 1,
            "parallel_infer": self.config.parallel_infer,
        }
        if self.config.speed_factor != 1.0:
            payload["speed_factor"] = self.config.speed_factor
        return payload


class OpenAISpeechProvider(_WAVHTTPProvider):
    """OpenAI-compatible static speech bytes from the managed local service."""

    route = "/v1/audio/speech"

    async def _payload(self, text):
        return {"model": self.config.model, "input": text, "voice": self.config.voice,
                "response_format": "wav"}


class FasterWhisperConfig(_Config):
    model_dir: Path = Field(repr=False)
    device: Literal["cuda", "cpu"] = "cuda"
    device_index: int = Field(default=0, ge=0, le=15, strict=True)
    compute_type: Literal["int8_float16", "float16", "int8", "float32"] = "int8_float16"
    cpu_threads: int = Field(default=4, ge=1, le=32, strict=True)
    max_concurrency: int = Field(default=1, ge=1, le=4, strict=True)
    beam_size: int = Field(default=1, ge=1, le=5, strict=True)
    vad_filter: bool = Field(default=True, strict=True)
    language: str | None = "zh"
    timeout_seconds: float = Field(default=90, gt=0, le=600, allow_inf_nan=False)
    max_segments: int = Field(default=128, ge=1, le=1000, strict=True)
    max_text_chars: int = Field(default=12000, ge=1, le=50000, strict=True)
    input: WAVLimits = Field(
        default_factory=lambda: WAVLimits(max_bytes=8_000_000, max_duration_seconds=120),
    )

    @field_validator("model_dir")
    @classmethod
    def absolute_model(cls, value):
        if not value.is_absolute():
            raise ValueError("model_dir must be an absolute local directory")
        return value

    @field_validator("language")
    @classmethod
    def valid_language(cls, value):
        if value is not None and not re.fullmatch(r"[a-z]{2,3}", value):
            raise ValueError("language must be an ASR language code or null")
        return value


@dataclass(frozen=True)
class TranscriptionSegment:
    start_seconds: float
    end_seconds: float
    text: str = field(repr=False)


@dataclass(frozen=True)
class TranscriptionResult:
    text: str = field(repr=False)
    segments: tuple[TranscriptionSegment, ...] = field(repr=False)
    language: str
    language_probability: float
    duration_seconds: float
    partial: bool


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class FasterWhisperProvider:
    """Synchronous ASR for a supervised worker. Consume every segment before success."""

    def __init__(
        self, config: FasterWhisperConfig, *, model_factory: Callable[..., Any] | None = None
    ):
        self.config = config
        self._model_factory = model_factory
        self._model = None
        self._model_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(config.max_concurrency)

    def _load_model(self):
        with self._model_lock:
            if self._model is not None:
                return self._model
            try:
                model_dir = self.config.model_dir.resolve(strict=True)
                # tokenizer.json is mandatory: upstream otherwise calls from_pretrained.
                if not model_dir.is_dir() or any(
                    not (model_dir / name).is_file() or (model_dir / name).stat().st_size == 0
                    for name in ("model.bin", "config.json", "tokenizer.json")
                ):
                    raise OSError
            except (OSError, RuntimeError):
                raise LocalVoiceUnavailable("VOICE_ASR_MODEL_UNAVAILABLE") from None
            factory = self._model_factory
            if factory is None:
                try:
                    factory = importlib.import_module("faster_whisper").WhisperModel
                except (ImportError, OSError, AttributeError):
                    raise LocalVoiceUnavailable("VOICE_ASR_DEPENDENCY_UNAVAILABLE") from None
            try:
                self._model = factory(
                    str(model_dir),
                    device=self.config.device,
                    device_index=self.config.device_index,
                    compute_type=self.config.compute_type,
                    cpu_threads=self.config.cpu_threads,
                    num_workers=self.config.max_concurrency,
                    local_files_only=True,
                )
            except Exception:
                raise LocalVoiceUnavailable("VOICE_ASR_MODEL_UNAVAILABLE") from None
            return self._model

    def transcribe(self, data: bytes, *, source_partial: bool = False) -> TranscriptionResult:
        if not isinstance(source_partial, bool):
            raise LocalVoiceError("VOICE_ASR_PARTIAL_INVALID")
        audio = validate_wav(data, self.config.input)
        if not self._slots.acquire(blocking=False):
            raise LocalVoiceError("VOICE_BUSY")
        deadline = time.monotonic() + self.config.timeout_seconds
        segments = None
        try:
            model = self._load_model()
            self._check_deadline(deadline)
            segments, info = model.transcribe(
                BytesIO(audio.audio),
                language=self.config.language,
                task="transcribe",
                beam_size=self.config.beam_size,
                best_of=1,
                temperature=0.0,
                vad_filter=self.config.vad_filter,
                condition_on_previous_text=False,
                word_timestamps=False,
                log_progress=False,
            )
            self._check_deadline(deadline)
            language, probability = info.language, info.language_probability
            if (
                not isinstance(language, str)
                or not re.fullmatch(r"[a-z]{2,3}", language)
                or not _finite_number(probability)
                or not 0 <= probability <= 1
            ):
                raise LocalVoiceError("VOICE_ASR_RESULT_INVALID")
            result, text_size, last_end = [], 0, 0.0
            for segment in segments:
                self._check_deadline(deadline)
                start, end, text = segment.start, segment.end, segment.text
                # Decoder timestamps are estimates in 20 ms steps. After VAD maps
                # them back, a short tail can extend past the captured PCM (observed
                # 5.62 s for a 5.56 s clip with faster-whisper 1.2.1). Permit at
                # most five steps of tail rounding, never invent source duration.
                if (
                    _finite_number(end)
                    and audio.duration_seconds < end <= audio.duration_seconds + 0.100000001
                ):
                    end = audio.duration_seconds
                if (
                    not _finite_number(start)
                    or not _finite_number(end)
                    or start < last_end
                    or end <= start
                    or end > audio.duration_seconds
                    or not isinstance(text, str)
                    or not text.strip()
                ):
                    raise LocalVoiceError("VOICE_ASR_RESULT_INVALID")
                text_size += len(text)
                if (
                    len(result) >= self.config.max_segments
                    or text_size > self.config.max_text_chars
                ):
                    raise LocalVoiceError("VOICE_ASR_RESULT_TOO_LARGE")
                result.append(TranscriptionSegment(float(start), float(end), text))
                last_end = end
            self._check_deadline(deadline)
            return TranscriptionResult(
                "".join(s.text for s in result).strip(),
                tuple(result),
                language,
                float(probability),
                audio.duration_seconds,
                source_partial,
            )
        except LocalVoiceError:
            raise
        except Exception:
            raise LocalVoiceError("VOICE_ASR_FAILED") from None
        finally:
            try:
                close = getattr(segments, "close", None)
                if close is not None:
                    close()
            except Exception:
                raise LocalVoiceError("VOICE_ASR_FAILED") from None
            finally:
                self._slots.release()

    @staticmethod
    def _check_deadline(deadline: float):
        if time.monotonic() >= deadline:
            raise LocalVoiceError("VOICE_ASR_TIMEOUT")
