"""Export a stopped native catalog under original custody, without opening a Spool writer."""

import argparse
import base64
import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from companion_meme_protocol import canonical, digest, source_evidence, strict_json, validate_bytes

from .config import BridgeConfig
from .instance_lock import acquire
from .meme_catalog import MemeCatalog
from .owner_runtime import OwnerRuntime

MAX_ROWS, MAX_BYTES, MAX_MANIFEST = 4096, 64 * 1024 * 1024, 100 * 1024 * 1024


def _linked(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return True
    if os.name == "nt":
        try:
            return bool(info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except AttributeError as error:
            raise ValueError("MEME_EXPORT_PATH_INVALID") from error
    return False


def _file(path):
    path = Path(path).absolute()
    if (any(_linked(p) for p in (path, *path.parents))
        or not stat.S_ISREG(path.stat().st_mode)):
        raise ValueError("MEME_EXPORT_PATH_INVALID")
    return path


class _ReadonlyRuntime(OwnerRuntime):
    def __init__(self, db, config, directory):
        # Reuse the original read-only verifier, never OwnerRuntime's constructor
        # (which creates tables and can stop uncertain in-flight work on restart).
        self.db, self.config, self.authority = db, config, config.owner_authority
        self.spool = SimpleNamespace(db=db, config=config, directory=directory,
                                     reconciliation_only=True)
        self.clock = lambda: datetime.now(UTC)
        self.path = _file(directory / "owner-journal.jsonl")
        self.stop_path = _file(directory / ("owner-" + digest(self.authority) + ".stop"))
        self.legacy = None
        self._valid_profiles = set()


def export(config, output):
    directory = Path(config.data_dir).absolute()
    database = _file(directory / "spool.sqlite3")
    lock_path = _file(directory / "instance.lock")
    output = Path(output).absolute()
    if (output.is_relative_to(directory) or output.exists()
        or any(_linked(p) for p in output.parents)
        or lock_path.stat().st_size < 1 or config.owner_authority is None):
        raise ValueError("MEME_EXPORT_PATH_INVALID")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists():
            _file(sidecar)
    # A running Bridge owns this exact OS lock. Failure never stops that process.
    with (acquire(lock_path),
          closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db):
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        expected = [config.owner_qq_id, config.bot_qq_id, config.origin, config.protocol_epoch]
        identity = db.execute("SELECT value FROM identity").fetchall()
        if len(identity) != 1 or json.loads(identity[0][0]) != expected:
            raise ValueError("MEME_EXPORT_CUSTODY_INVALID")
        runtime = _ReadonlyRuntime(db, config, directory)
        runtime.verify(active=False)
        row = runtime.row()
        if row["state"] != "STOPPED" or runtime.stop_path.read_bytes() != b"OWNER_STOP\n":
            raise ValueError("MEME_EXPORT_STOP_REQUIRED")
        count, size = db.execute("SELECT COUNT(*),COALESCE(SUM(length(data)),0) "
                                 "FROM meme_assets").fetchone()
        if count > MAX_ROWS or size > MAX_BYTES:
            raise ValueError("MEME_EXPORT_LIMIT")
        assets = []
        for sha, data, raw, active in db.execute(
            "SELECT sha,data,metadata,active FROM meme_assets ORDER BY sha"
        ):
            if active not in (0, 1) or not isinstance(sha, str) or not re.fullmatch(
                r"[a-f0-9]{64}", sha
            ):
                raise ValueError("MEME_EXPORT_ASSET_INVALID")
            metadata = MemeCatalog.metadata(canonical(strict_json(raw)))
            image = validate_bytes(data) if active else None
            if active and (image is None or image.sha256 != sha):
                raise ValueError("MEME_EXPORT_ASSET_INVALID")
            if not active and data is not None:
                raise ValueError("MEME_EXPORT_ASSET_INVALID")
            item = {"asset_sha256": sha, "source_sha256": digest(source_evidence(metadata)),
                    "metadata": json.loads(canonical(metadata.__dict__)), "active": bool(active),
                    "mime": image.mime_type if image else None,
                    "data_base64": base64.b64encode(data).decode() if active else None}
            assets.append({**item, "sha256": digest(item)})
        runtime.verify(active=False)
        if runtime.row() != row:
            raise ValueError("MEME_EXPORT_CUSTODY_CHANGED")
        manifest = {"schema_version": 1, "custody": {"identity": expected,
                    "capture_public_key": config.capture_public_key,
                    "authority": config.owner_authority, "journal_head": row["journal_head"]},
                    "assets": assets}
        manifest["sha256"] = digest(manifest)
        encoded = (canonical(manifest) + "\n").encode()
        if len(encoded) > MAX_MANIFEST:
            raise ValueError("MEME_EXPORT_LIMIT")
        # One exclusive private artifact, not an extracted directory or spool copy.
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return {"manifest_sha256": manifest["sha256"], "assets": len(assets), "bytes": size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = export(BridgeConfig.load(args.config), args.output)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        print(json.dumps({"error": "MEME_EXPORT_REFUSED"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
