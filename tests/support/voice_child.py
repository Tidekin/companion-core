"""Synthetic provider behind the native supervised-child handshake."""

import base64
import json
import os
import sys
import time
from pathlib import Path

from companion_voice_node.supervisor import child_entry


def fixture_provider(packet):
    fixture = packet.pop("_fixture")
    with Path(fixture["counter"]).open("a", encoding="utf-8") as output:
        output.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "environment_keys": sorted(os.environ),
                    "packet_keys": sorted(packet),
                    "provider_keys": sorted(packet["provider"]),
                }
            )
            + "\n"
        )
    if fixture["scenario"] == "block":
        time.sleep(60)
        raise RuntimeError("blocked fixture should have been terminated")
    if fixture["scenario"] == "crash":
        os._exit(3)
    if fixture["scenario"] == "bad-result":
        return {"status": "SUCCEEDED", "payload": {"kind": "tts", "private": "do not echo"}}
    if fixture["scenario"] == "false-not-started":
        return {"status": "FAILED", "remote_state": "NOT_STARTED", "error_code": "UNPROVEN_FAILURE"}
    from companion.voice_protocol import VoiceAudioResult
    from companion_voice_node.providers import validate_wav
    from tests.voice_service_fixtures import wav_bytes

    if packet["operation"] == "tts":
        audio = validate_wav(
            wav_bytes(45, rate=44100) if fixture["scenario"] == "large-result" else wav_bytes(0.125)
        )
        payload = VoiceAudioResult(
            audio_base64=base64.b64encode(audio.audio).decode(),
            audio_sha256=audio.sha256,
            sample_rate=audio.sample_rate,
            channels=1,
            sample_width=2,
            frames=audio.frames,
            duration_seconds=audio.duration_seconds,
        ).model_dump(mode="json")
    else:
        audio = validate_wav(base64.b64decode(packet["input_base64"]))
        payload = {
            "kind": "asr",
            "text": "测试结果",
            "segments": [
                {
                    "start_seconds": 0.0,
                    "end_seconds": audio.duration_seconds,
                    "text": "测试结果",
                }
            ],
            "language": "zh",
            "language_probability": 0.9,
            "duration_seconds": audio.duration_seconds,
            "partial": packet["source_partial"],
        }
    return {"status": "SUCCEEDED", "payload": payload}


if __name__ == "__main__":
    raise SystemExit(child_entry(int(sys.argv[1]), runner=fixture_provider))
