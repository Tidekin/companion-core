import math
import struct
import wave
from dataclasses import replace
from io import BytesIO

import pytest

from companion_voice_node.singing_audio import RATE, bubble_manifest, split_song_pcm


def tone(seconds, amplitude=5000):
    cycle = b"".join(struct.pack("<h", round(amplitude * math.sin(2 * math.pi * i / 80)))
                     for i in range(80))
    frames = round(seconds * RATE)
    return (cycle * ((frames + 79) // 80))[:frames * 2]


@pytest.mark.parametrize("seconds", [0.001, 54.999, 55, 55.001, 110, 134.117, 600])
def test_whole_song_survives_record_limits_and_reconstruction(seconds):
    pcm = tone(seconds)
    parts = split_song_pcm(pcm)
    manifest = bubble_manifest(pcm, parts)
    reconstructed = []
    for part in parts:
        assert 0 < part.audio.duration_seconds <= 55
        assert len(part.audio.audio) <= 4_000_000
        with wave.open(BytesIO(part.audio.audio), "rb") as audio:
            reconstructed.append(audio.readframes(audio.getnframes()))
    assert b"".join(reconstructed) == pcm
    assert manifest["source_frames"] == len(pcm) // 2
    assert manifest["segments"][-1]["end_frame"] == len(pcm) // 2


def test_isolated_vocal_pause_sets_cut_without_removing_backing_audio():
    pcm = tone(100)
    vocal = tone(48) + bytes(2 * RATE) + tone(51)
    parts = split_song_pcm(pcm, pause_pcm=vocal)
    assert 48 * RATE < parts[0].end_frame < 49 * RATE
    assert parts[1].start_frame == parts[0].end_frame
    bubble_manifest(pcm, parts)


@pytest.mark.parametrize("kind", ["empty", "odd", "oversize", "non_bytes"])
def test_unbounded_or_malformed_pcm_is_rejected(kind):
    pcm = {"empty": b"", "odd": b"x", "non_bytes": "audio"}.get(kind)
    if kind == "oversize":
        pcm = bytes(RATE * 600 * 2 + 2)
    with pytest.raises(ValueError):
        split_song_pcm(pcm)


def test_pause_track_must_have_the_same_coverage():
    with pytest.raises(ValueError, match="same frames"):
        split_song_pcm(tone(2), pause_pcm=tone(1))


@pytest.mark.parametrize("tamper", ["order", "gap", "duplicate", "missing", "content"])
def test_delivery_manifest_rejects_nonpartitioned_or_changed_content(tamper):
    pcm = tone(120)
    parts = split_song_pcm(pcm)
    if tamper == "order":
        parts = tuple(reversed(parts))
    elif tamper == "gap":
        parts = (replace(parts[0], end_frame=parts[0].end_frame - 1), *parts[1:])
    elif tamper == "duplicate":
        parts = (*parts, parts[-1])
    elif tamper == "missing":
        parts = parts[:-1]
    else:
        pcm = bytes(len(pcm))
    with pytest.raises(ValueError):
        bubble_manifest(pcm, parts)
