"""Pure reply-group hashing shared by Core and the independent Bridge."""

from . import digest


def part_hash(content, version):
    return digest(content if version == 2 else content["text"])


def manifest(job_id, hashes, total_chars, interval_ms, version=1):
    value = {"job_id": job_id, "parts_sha256": hashes,
             "interval_ms": interval_ms, "total_chars": total_chars}
    if version == 2:
        value["version"] = 2
    return value
