import hashlib
from io import BytesIO

import av
import numpy as np
import pytest

from companion_voice_node.providers import LocalVoiceError
from companion_voice_node.singing_decode import decode_song


def aac():
    encoded = BytesIO()
    with av.open(encoded, mode="w", format="mp4") as container:
        stream = container.add_stream("aac", rate=44100)
        stream.layout = "stereo"
        values = np.zeros((2, 4410), dtype=np.float32)
        frame = av.AudioFrame.from_ndarray(values, format="fltp", layout="stereo")
        frame.sample_rate = 44100
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return encoded.getvalue()


def test_real_offline_aac_decoder_produces_bounded_complete_pcm(tmp_path):
    source, output = tmp_path / "source.m4a", tmp_path / "output.wav"
    data = aac()
    source.write_bytes(data)
    result = decode_song(source, output, expected_sha256=hashlib.sha256(data).hexdigest())
    assert result["sample_rate"] == 44100 and result["channels"] == 2
    assert 0.09 < result["duration_seconds"] < 0.2
    assert output.stat().st_size == 44 + result["frames"] * 4


def test_changed_source_is_rejected_before_decoding(tmp_path):
    source = tmp_path / "changed.m4a"
    source.write_bytes(b"changed bytes")
    with pytest.raises(LocalVoiceError, match="SINGING_DECODE_SOURCE_CHANGED"):
        decode_song(source, tmp_path / "output.wav", expected_sha256="a" * 64)
    assert not (tmp_path / "output.wav").exists()


def test_playlist_or_external_reference_is_not_a_media_format(tmp_path):
    source = tmp_path / "playlist.m4a"
    data = b"#EXTM3U\nhttp://127.0.0.1/private\n"
    source.write_bytes(data)
    with pytest.raises(LocalVoiceError, match="SINGING_DECODE_FORMAT_INVALID"):
        decode_song(source, tmp_path / "output.wav",
                    expected_sha256=hashlib.sha256(data).hexdigest())
