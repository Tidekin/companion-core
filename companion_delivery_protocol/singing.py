"""Pure full-song manifest and part identity, shared with the independent Bridge."""

import hashlib
import json
import re

RATE = 32000
MAX_FRAMES = RATE * 600
MAX_SEGMENTS = 24
MAX_RESULT_BODY_BYTES = 56 * 1024 * 1024
SHA = re.compile(r"[a-f0-9]{64}")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("invalid singing integer")


def _hash(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError("invalid singing digest")


def validate_manifest(value):
    if (not isinstance(value, dict) or set(value) != {
        "version", "node_manifest_sha256", "source", "sample_rate", "channels",
        "sample_width", "frames", "pcm_sha256", "segments"}
        or type(value["version"]) is not int or value["version"] != 1
        or type(value["sample_rate"]) is not int or value["sample_rate"] != RATE
        or type(value["channels"]) is not int or value["channels"] != 1
        or type(value["sample_width"]) is not int or value["sample_width"] != 2):
        raise ValueError("invalid singing manifest")
    _hash(value["node_manifest_sha256"])
    _hash(value["pcm_sha256"])
    _integer(value["frames"], 1, MAX_FRAMES)
    source = value["source"]
    if (not isinstance(source, dict) or set(source) != {
        "bvid", "cid", "part", "duration_milliseconds", "audio_sha256", "audio_bytes",
        "view_response_sha256", "nav_response_sha256", "playinfo_response_sha256",
        "representation_sha256", "locator_sha256", "used_requests", "used_bytes"}
        or not isinstance(source["bvid"], str)
        or not re.fullmatch(r"BV[0-9A-Za-z]{10}", source["bvid"])):
        raise ValueError("invalid singing source")
    _integer(source["cid"], 1, 2**63 - 1)
    _integer(source["part"], 1, 100)
    _integer(source["duration_milliseconds"], 1, 600000)
    _integer(source["audio_bytes"], 1, 32 * 1024 * 1024)
    _integer(source["used_requests"], 4, 4)
    _integer(source["used_bytes"], source["audio_bytes"], 34 * 1024 * 1024)
    for key in ("audio_sha256", "view_response_sha256", "nav_response_sha256",
                "playinfo_response_sha256", "representation_sha256", "locator_sha256"):
        _hash(source[key])
    if abs(value["frames"] / RATE - source["duration_milliseconds"] / 1000) > 1:
        raise ValueError("singing source coverage differs")
    parts = value["segments"]
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_SEGMENTS:
        raise ValueError("invalid singing segment count")
    offset = 0
    for index, part in enumerate(parts):
        if not isinstance(part, dict) or set(part) != {
            "index", "start_frame", "end_frame", "audio_sha256", "audio_bytes"}:
            raise ValueError("invalid singing segment")
        _integer(part["index"], index, index)
        _integer(part["start_frame"], offset, offset)
        _integer(part["end_frame"], offset + 1, min(offset + RATE * 55, value["frames"]))
        _integer(part["audio_bytes"], 44, 4_000_000)
        _hash(part["audio_sha256"])
        offset = part["end_frame"]
    if offset != value["frames"]:
        raise ValueError("singing segments do not cover the whole song")
    return value


def validate_part(manifest, index, audio):
    """Match one separately delivered PCM WAV's metadata to the full manifest."""
    validate_manifest(manifest)
    _integer(index, 0, len(manifest["segments"]) - 1)
    part = manifest["segments"][index]
    if (audio["audio_sha256"] != part["audio_sha256"]
        or audio["audio_bytes"] != part["audio_bytes"]
        or audio["frames"] != part["end_frame"] - part["start_frame"]
        or audio["sample_rate"] != RATE or audio["channels"] != 1
        or audio["sample_width"] != 2):
        raise ValueError("singing audio differs from the selected segment")
    return part
