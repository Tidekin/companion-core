"""Bounded offline AAC-to-PCM conversion for a supervised singing child process."""

import hashlib
import wave
from io import BytesIO
from pathlib import Path

from companion_voice_node.providers import LocalVoiceError
from source_reader.bili_audio import MAX_SONG_BYTES, MAX_SONG_SECONDS


def decode_song(source: Path, destination: Path, *, expected_sha256: str,
                max_seconds=MAX_SONG_SECONDS, max_bytes=MAX_SONG_BYTES):
    """Decode one frozen local audio MP4; no URL or external media references."""
    if (not source.is_absolute() or source.resolve() != source.absolute()
        or source.is_symlink() or not 1 <= source.stat().st_size <= max_bytes
        or not destination.is_absolute() or destination.resolve() != destination.absolute()
        or destination.exists() or str(source).startswith(("\\\\", "//"))
        or str(destination).startswith(("\\\\", "//"))):
        raise LocalVoiceError("SINGING_DECODE_INPUT_INVALID")
    data = source.read_bytes()
    if len(data) > max_bytes or hashlib.sha256(data).hexdigest() != expected_sha256:
        raise LocalVoiceError("SINGING_DECODE_SOURCE_CHANGED")
    import av

    frames = 0
    try:
        with av.open(BytesIO(data), mode="r", format="mov", options={
            "protocol_whitelist": "", "enable_drefs": "0", "use_absolute_path": "0",
        }) as incoming, destination.open("xb") as target, wave.open(target, "wb") as output:
            if (len(incoming.streams) != 1 or len(incoming.streams.audio) != 1
                or incoming.streams.audio[0].codec_context.name != "aac"):
                raise LocalVoiceError("SINGING_DECODE_FORMAT_INVALID")
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(44100)
            resampler = av.AudioResampler(format="s16", layout="stereo", rate=44100)

            def write(converted):
                nonlocal frames
                frames += converted.samples
                if frames > max_seconds * 44100:
                    raise LocalVoiceError("SINGING_DECODE_DURATION_EXCEEDED")
                output.writeframesraw(converted.to_ndarray().tobytes())

            for frame in incoming.decode(incoming.streams.audio[0]):
                for converted in resampler.resample(frame):
                    write(converted)
            for converted in resampler.resample(None):
                write(converted)
    except (av.error.FFmpegError, ValueError, OverflowError):
        raise LocalVoiceError("SINGING_DECODE_FORMAT_INVALID") from None
    if not frames:
        raise LocalVoiceError("SINGING_DECODE_EMPTY")
    return {"sample_rate": 44100, "channels": 2, "frames": frames,
            "duration_seconds": frames / 44100}
