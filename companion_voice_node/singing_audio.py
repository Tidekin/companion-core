"""Lossless PCM segmentation for ordered singing bubbles; no I/O or delivery policy."""

import hashlib
import math
import sys
import wave
from array import array
from dataclasses import dataclass
from io import BytesIO

from companion_voice_node.providers import ValidatedAudio, WAVLimits, validate_wav

RATE = 32000
MAX_SONG_SECONDS = 600
MAX_BUBBLE_SECONDS = 55
MAX_BUBBLE_BYTES = 4_000_000


@dataclass(frozen=True)
class SongBubble:
    index: int
    start_frame: int
    end_frame: int
    audio: ValidatedAudio


def _samples(pcm):
    if (not isinstance(pcm, bytes) or not pcm or len(pcm) % 2
        or len(pcm) > RATE * MAX_SONG_SECONDS * 2):
        raise ValueError("singing requires bounded mono 32 kHz PCM16")
    result = array("h")
    result.frombytes(pcm)
    if sys.byteorder != "little":
        result.byteswap()
    return result


def _pause(samples, lower, upper):
    """Prefer the last four quiet 20 ms windows near the requested boundary."""
    hop, quiet, run, found = RATE // 50, 328, 0, None
    for offset in range(lower, upper - hop + 1, hop):
        values = samples[offset:offset + hop]
        rms = math.sqrt(sum(value * value for value in values) / hop)
        run = run + 1 if rms <= quiet else 0
        if run >= 4:
            found = offset - hop  # Inside the pause, with quiet audio on either side.
    return found


def split_song_pcm(pcm: bytes, *, pause_pcm: bytes | None = None) -> tuple[SongBubble, ...]:
    """Cover every source frame exactly once, optionally cutting on isolated-vocal pauses.

    The caller supplies normalized 32 kHz mono PCM16. This function applies no
    fades, overlap, normalization, trimming or stretching. Every returned WAV fits
    the existing native record envelope; order and send acknowledgment stay in Core.
    """
    samples = _samples(pcm)
    if pause_pcm is not None:
        pauses = _samples(pause_pcm)
        if len(pauses) != len(samples):
            raise ValueError("pause track must cover the same frames")
    else:
        pauses = samples
    total, start, result = len(samples), 0, []
    maximum = min(RATE * MAX_BUBBLE_SECONDS, (MAX_BUBBLE_BYTES - 44) // 2)
    while start < total:
        end = min(start + maximum, total)
        if end < total:
            # Preserve at least five seconds in the final bubble when feasible.
            if total - end < 5 * RATE:
                end -= 5 * RATE - (total - end)
            boundary = _pause(pauses, max(start + RATE, end - 10 * RATE), end)
            if boundary is not None:
                end = boundary
        frames = end - start
        stream = BytesIO()
        with wave.open(stream, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(RATE)
            output.writeframes(pcm[start * 2:end * 2])
        audio = validate_wav(stream.getvalue(), WAVLimits(
            max_bytes=MAX_BUBBLE_BYTES, max_duration_seconds=MAX_BUBBLE_SECONDS,
            sample_rates=(RATE,), channels=1))
        if audio.frames != frames:
            raise ValueError("singing bubble frame identity changed")
        result.append(SongBubble(len(result), start, end, audio))
        start = end
    return tuple(result)


def bubble_manifest(pcm: bytes, bubbles: tuple[SongBubble, ...]) -> dict:
    """Describe exact source coverage without embedding the original audio."""
    expected, offset = len(pcm) // 2, 0
    if not bubbles:
        raise ValueError("singing bubbles required")
    for index, bubble in enumerate(bubbles):
        if (bubble.index != index or bubble.start_frame != offset
            or bubble.end_frame - bubble.start_frame != bubble.audio.frames
            or not bubble.start_frame < bubble.end_frame <= expected):
            raise ValueError("singing bubbles must form a complete ordered partition")
        with wave.open(BytesIO(bubble.audio.audio), "rb") as audio:
            if audio.readframes(bubble.audio.frames) != pcm[offset * 2:bubble.end_frame * 2]:
                raise ValueError("singing bubble content changed")
        offset = bubble.end_frame
    if offset != expected:
        raise ValueError("singing bubbles must cover the complete song")
    return {"sample_rate": RATE, "channels": 1, "sample_width": 2,
        "source_pcm_sha256": hashlib.sha256(pcm).hexdigest(), "source_frames": expected,
        "duration_seconds": expected / RATE,
        "segments": [{"index": part.index, "start_frame": part.start_frame,
            "end_frame": part.end_frame, "audio_sha256": part.audio.sha256,
            "bytes": len(part.audio.audio), "duration_seconds": part.audio.duration_seconds}
            for part in bubbles]}
