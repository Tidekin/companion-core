import base64
import hashlib

from companion.voice_protocol import SingingAudio, SingingResult
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
