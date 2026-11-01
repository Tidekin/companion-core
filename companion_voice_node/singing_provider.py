"""Offline local singing child; Core credentials and platform locators stay in the parent."""

import base64
from pathlib import Path

from companion.singing_protocol import SingingRecipe
from companion.voice_protocol import SingingAudio, SingingResult
from companion_voice_node.applio_render import ApplioRenderConfig, render
from companion_voice_node.providers import validate_wav
from companion_voice_node.singing_decode import decode_song


def execute(packet):
    recipe = SingingRecipe.model_validate_json(
        base64.b64decode(packet["input_base64"], validate=True))
    config = ApplioRenderConfig.model_validate(packet["provider"])
    source = Path(packet["song_path"])
    proof = packet["song_source"]
    if (proof["bvid"] != recipe.bvid or proof["part"] != recipe.part
        or proof["audio_bytes"] > recipe.max_input_bytes
        or proof["duration_milliseconds"] > recipe.max_seconds * 1000):
        raise ValueError("SINGING_SOURCE_CHANGED")
    decoded = source.with_suffix(".wav")
    decode_song(source, decoded, expected_sha256=proof["audio_sha256"],
                max_seconds=recipe.max_seconds, max_bytes=recipe.max_input_bytes)
    output = source.parent / "render"
    receipt = render(config, decoded, output)
    audio = []
    segments = []
    for part in receipt["segments"]:
        data = (output / f"bubble-{part['index'] + 1:02d}.wav").read_bytes()
        info = validate_wav(data)
        audio.append(SingingAudio(audio_base64=base64.b64encode(data).decode(),
            audio_sha256=info.sha256, sample_rate=info.sample_rate, channels=info.channels,
            sample_width=info.sample_width, frames=info.frames,
            duration_seconds=info.duration_seconds))
        segments.append({key: part[key] for key in
                         ("index", "start_frame", "end_frame", "audio_sha256")}
                        | {"audio_bytes": len(data)})
    manifest = {"version": 1, "node_manifest_sha256": packet["node_manifest_sha256"],
        "source": proof, "sample_rate": 32000, "channels": 1, "sample_width": 2,
        "frames": receipt["source_frames"], "pcm_sha256": receipt["source_pcm_sha256"],
        "segments": segments}
    return SingingResult(manifest=manifest, audio=tuple(audio))
