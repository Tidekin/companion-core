import base64
import copy
import hashlib

import pytest
from pydantic import ValidationError

from companion.voice_protocol import SingingAudio, SingingResult, digest, result_digest
from companion_delivery_protocol import singing
from companion_voice_node.singing_audio import split_song_pcm


def result(seconds=60):
    pcm = b"\x01\x00" * 32000 * seconds
    bubbles = split_song_pcm(pcm)
    manifest = {"version": 1, "node_manifest_sha256": "a" * 64, "source": {
        "bvid": "BV1234567890", "cid": 123, "part": 1,
        "duration_milliseconds": seconds * 1000,
        "audio_sha256": "b" * 64, "audio_bytes": 1000, "used_requests": 4, "used_bytes": 2000,
        **{key: "c" * 64 for key in ("view_response_sha256", "nav_response_sha256",
            "playinfo_response_sha256", "representation_sha256", "locator_sha256")}},
        "sample_rate": 32000, "channels": 1, "sample_width": 2, "frames": len(pcm) // 2,
        "pcm_sha256": hashlib.sha256(pcm).hexdigest(), "segments": [{
            "index": part.index, "start_frame": part.start_frame, "end_frame": part.end_frame,
            "audio_sha256": part.audio.sha256, "audio_bytes": len(part.audio.audio)}
            for part in bubbles]}
    audio = tuple(SingingAudio(audio_base64=base64.b64encode(part.audio.audio).decode(),
        audio_sha256=part.audio.sha256, sample_rate=32000, channels=1,
        frames=part.audio.frames, duration_seconds=part.audio.duration_seconds) for part in bubbles)
    return SingingResult(manifest=manifest, audio=audio)


def test_complete_song_manifest_is_compact_and_independently_verifiable():
    song = result()
    assert result_digest(song) == digest(song.manifest) == singing.digest(song.manifest)
    for index, audio in enumerate(song.audio):
        metadata = {"audio_bytes": len(audio.audio_bytes()),
                    **audio.model_dump(exclude={"audio_base64"})}
        assert singing.validate_part(song.manifest, index, metadata)["index"] == index
    assert len(song.model_dump_json()) > 5_000_000


@pytest.mark.parametrize("change", ["order", "gap", "frames", "bytes", "pcm_hash", "duration",
                                    "source", "node", "index_type"])
def test_changed_or_incomplete_song_manifest_is_rejected(change):
    song = result()
    value = copy.deepcopy(song.model_dump(mode="json"))
    manifest = value["manifest"]
    if change == "order":
        value["audio"].reverse()
    elif change == "gap":
        manifest["segments"][1]["start_frame"] += 1
    elif change == "frames":
        manifest["frames"] += 1
    elif change == "bytes":
        manifest["segments"][0]["audio_bytes"] -= 1
    elif change == "pcm_hash":
        manifest["pcm_sha256"] = "0" * 64
    elif change == "duration":
        manifest["source"]["duration_milliseconds"] = 300000
    elif change == "source":
        manifest["source"]["bvid"] = "https://127.0.0.1/private"
    elif change == "node":
        manifest["node_manifest_sha256"] = "not-a-manifest"
    else:
        manifest["segments"][0]["index"] = False
    with pytest.raises(ValidationError):
        SingingResult.model_validate(value)
