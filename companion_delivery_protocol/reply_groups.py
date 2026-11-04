"""Pure reply-group hashing shared by Core and the independent Bridge."""

import re

from . import digest
from .singing import MAX_SEGMENTS


def part_hash(content, version):
    return digest(content if version in (2, 3) else content["text"])


def manifest(job_id, hashes, total_chars, interval_ms, version=1):
    value = {"job_id": job_id, "parts_sha256": hashes,
             "interval_ms": interval_ms, "total_chars": total_chars}
    if version in (2, 3):
        value["version"] = version
    return value


def mixed_part(content):
    """Validate a content-bound per-bubble choice, without granting asset authority."""
    if not isinstance(content, dict) or not isinstance(content.get("text"), str):
        raise ValueError("INVALID_MIXED_REPLY_PART")
    choice = content.get("reply_part")
    if not isinstance(choice, dict):
        raise ValueError("INVALID_MIXED_REPLY_PART")
    kind = choice.get("kind")
    fields = {"kind", "source_index"}
    if kind == "singing":
        fields |= {"request_id", "singing_segment_index"}
    if (kind not in {"text", "voice", "meme", "generated_image", "singing"}
        or set(choice) != fields or type(choice.get("source_index")) is not int
        or not 0 <= choice["source_index"] < 32):
        raise ValueError("INVALID_MIXED_REPLY_PART")
    expected = {"text", "reply_part"}
    if kind in {"meme", "generated_image"}:
        expected.add("image" if kind == "meme" else "generated_image")
        if content["text"] != "":
            raise ValueError("INVALID_MIXED_REPLY_PART")
    elif not content["text"].strip():
        raise ValueError("INVALID_MIXED_REPLY_PART")
    if kind == "text" and "reply_to" in content:
        expected.add("reply_to")
    if set(content) != expected:
        raise ValueError("INVALID_MIXED_REPLY_PART")
    if kind == "singing" and (
        not isinstance(choice["request_id"], str)
        or not re.fullmatch(r"voice_[a-f0-9]{32}", choice["request_id"])
        or type(choice["singing_segment_index"]) is not int
        or not 0 <= choice["singing_segment_index"] < MAX_SEGMENTS
    ):
        raise ValueError("INVALID_MIXED_REPLY_PART")
    return choice
