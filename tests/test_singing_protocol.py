import copy

import pytest
from pydantic import ValidationError

from companion.voice_protocol import SingingResult, digest, result_digest
from companion_delivery_protocol import singing
from tests.support.singing_protocol import result


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
