"""Short-lived originals shared by API and worker; never part of the event journal."""

import json
import os
import re
import stat
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path

from companion.contracts import CoreError
from companion.privacy import epoch
from companion.store import encode
from companion_delivery_protocol.authority import MAX_OWNER_IMAGE_BYTES

# Covers one vision pass, bounded memory review, and final answer; never history.
RETENTION_SECONDS = 300
FILE_MIMES = frozenset({
    "application/pdf", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain", "text/markdown", "application/octet-stream",
})


def directory(store):
    database = Path(store.path).resolve()
    return database.parent / (database.name + ".owner-images")


def _path(store, item_id):
    if not re.fullmatch(r"content_[a-f0-9]{32}", item_id):
        raise CoreError("LIVE_IMAGE_ORIGINAL_CHANGED", 403)
    root = directory(store)
    if root.is_symlink() or (root.exists() and (
        root.resolve() != root or not root.is_dir()
        or (os.name != "nt" and root.stat().st_mode & 0o077)
    )):
        raise CoreError("LIVE_IMAGE_ORIGINAL_CHANGED", 403)
    target = root / (item_id + ".raw")
    if target.is_symlink():
        raise CoreError("LIVE_IMAGE_ORIGINAL_CHANGED", 403)
    return target


def _sync_directory(root):
    if os.name != "nt":
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _open(target):
    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(descriptor)
        current = target.lstat()
        if (
            not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(current.st_mode)
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
            or info.st_nlink != 1
            or (os.name != "nt" and info.st_mode & 0o077)
        ):
            raise ValueError()
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError()
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() != timedelta(0) or stamp.isoformat(timespec="microseconds") != value:
        raise ValueError()
    return stamp


def _file_limits(row):
    value = json.loads(row["attachment_json"])
    retention, size = value["spool_retention_seconds"], value["max_bytes"]
    if (type(retention) is not int or not 1 <= retention <= 86400
        or type(size) is not int or not 1 <= size <= 128 * 1024 * 1024):
        raise ValueError()
    return retention, size


def _header(stream, row):
    line = stream.readline(512)
    if not line.endswith(b"\n"):
        raise ValueError()
    value = json.loads(line)
    is_file = row.get("kind") == "qq_file"
    retention = _file_limits(row)[0] if is_file else RETENTION_SECONDS
    mimes = FILE_MIMES if is_file else {
        "image/png", "image/jpeg", "image/webp", "image/gif", "image/apng",
    }
    if (
        not isinstance(value, dict) or set(value) != {"mime", "expires_at", "privacy_epoch"}
        or value["mime"] not in mimes
        or type(value["privacy_epoch"]) is not int
        or value["privacy_epoch"] != row["captured_privacy_epoch"]
    ):
        raise ValueError()
    expires = _timestamp(value["expires_at"])
    # Upload must precede its content deadline. A modified header cannot extend
    # retention beyond that deadline plus the fixed original-image lifetime.
    if (
        expires <= datetime.fromisoformat(row["created_at"])
        or expires > datetime.fromisoformat(row["deadline"]) + timedelta(seconds=retention)
    ):
        raise ValueError()
    return value


def put(store, row, data, mime, expires_at):
    if row.get("kind") == "qq_file":
        retention, max_bytes = _file_limits(row)
        expires = _timestamp(expires_at)
        if (mime not in FILE_MIMES or not data or len(data) > max_bytes
            or expires <= datetime.fromisoformat(row["created_at"])
            or expires > datetime.fromisoformat(row["deadline"]) + timedelta(seconds=retention)):
            raise CoreError("FILE_ORIGINAL_INVALID", 400)
    target = _path(store, row["item_id"])
    target.parent.mkdir(mode=0o700, exist_ok=True)
    target = _path(store, row["item_id"])
    header = encode({
        "mime": mime, "expires_at": expires_at,
        "privacy_epoch": row["captured_privacy_epoch"],
    }).encode() + b"\n"
    # Exclusive writes are one-shot. A partial/crashed upload cannot be retried.
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(header)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(target.parent)
        _sync_directory(target.parent.parent)
    except BaseException:
        # Never remove another attempt's file when exclusive creation was denied.
        if "descriptor" in locals():
            target.unlink(missing_ok=True)
            _sync_directory(target.parent)
        raise


def get(store, row, now, max_bytes):
    target = _path(store, row["item_id"])
    try:
        with _open(target) as stream:
            if os.fstat(stream.fileno()).st_size > max_bytes + 512:
                raise ValueError()
            header = _header(stream, row)
            data = stream.read(max_bytes + 1)
        if (
            _timestamp(header["expires_at"]) <= datetime.fromisoformat(now)
            or not data or len(data) > max_bytes
        ):
            raise ValueError()
        return data, header["mime"]
    except (OSError, ValueError, TypeError, KeyError):
        raise CoreError("LIVE_IMAGE_ORIGINAL_UNAVAILABLE", 410) from None


def deadline(store, row):
    try:
        with _open(_path(store, row["item_id"])) as stream:
            return _header(stream, row)["expires_at"]
    except (OSError, ValueError, TypeError, KeyError):
        raise CoreError("LIVE_IMAGE_ORIGINAL_UNAVAILABLE", 410) from None


def discard(store, item_ids):
    for item_id in item_ids:
        target = _path(store, item_id)
        target.unlink(missing_ok=True)
        if target.parent.exists():
            _sync_directory(target.parent)


def discard_window(store, plan_sha256):
    for row in store.rows(
        "SELECT c.item_id,c.kind,r.value FROM content_items c JOIN runtime_meta r "
        "ON r.key='live_text_input:' || c.owner_event_id WHERE c.kind IN ('qq_image','qq_file')",
    ):
        if json.loads(row["value"]).get("window_sha256") == plan_sha256:
            discard(store, [row["item_id"]])
            store.execute(
                "DELETE FROM runtime_meta WHERE key=?",
                (("live_text_file_upload:" if row["kind"] == "qq_file"
                  else "live_text_image_upload:") + row["item_id"],),
            )


def cleanup(store, now, max_bytes=MAX_OWNER_IMAGE_BYTES, *, retain_completed=False):
    root = directory(store)
    if not root.exists():
        return
    if root.is_symlink() or root.resolve() != root:
        raise CoreError("LIVE_IMAGE_ORIGINAL_CHANGED", 403)
    # put() runs under the event's write transaction. Serialize startup/recovery
    # cleanup with it so another API process cannot delete an uncommitted upload.
    with nullcontext() if store.db.in_transaction else store.transaction():
        for path in root.glob("content_*.raw"):
            item_id = path.stem
            if not re.fullmatch(r"content_[a-f0-9]{32}", item_id):
                continue
            row = store.one(
                "SELECT c.*,j.state AS job_state,e.redacted AS source_redacted "
                "FROM content_items c JOIN events e ON e.event_id=c.owner_event_id "
                "JOIN jobs j ON j.event_id=c.owner_event_id AND j.kind='chat' "
                "WHERE c.item_id=?", (item_id,),
            )
            captured = bool(retain_completed and row and store.one(
                "SELECT 1 FROM captured_images WHERE item_id=? AND expires_at>?", (item_id, now),
            ))
            pending_file = bool(row and row["kind"] == "qq_file"
                and row["state"] in {"QUEUED", "RUNNING"} and row["deadline"] > now
                and store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                              ("owner_input:" + row["owner_event_id"],)))
            expired = (
                not row or row["redacted"] or row["source_redacted"]
                or (row["captured_privacy_epoch"] != epoch(store) and not captured)
                or row["state"] not in {"QUEUED", "RUNNING", "SUCCEEDED"}
                or (row["job_state"] not in {"QUEUED", "RUNNING", "RETRY_WAIT"}
                    and not captured and not pending_file)
            )
            if not expired:
                try:
                    limit = _file_limits(row)[1] if row["kind"] == "qq_file" else max_bytes
                    get(store, row, now, limit)
                except (CoreError, ValueError, KeyError, TypeError):
                    expired = True
            if expired:
                discard(store, [item_id])
