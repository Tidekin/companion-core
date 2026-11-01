"""Read-only identities for operator-controlled local model resources."""

import hashlib
from pathlib import Path

from companion.voice_protocol import VoiceProtocolError


def file_digest(path: Path) -> dict:
    try:
        if not path.is_file() or path.is_symlink():
            raise OSError
        before = path.stat()
        if not 0 < before.st_size <= 32_000_000_000:
            raise OSError
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                hasher.update(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise OSError
        return {"sha256": hasher.hexdigest(), "bytes": after.st_size}
    except OSError:
        raise VoiceProtocolError("VOICE_MANIFEST_RESOURCE_UNAVAILABLE") from None
