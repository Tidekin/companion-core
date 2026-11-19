"""Owner-chosen avatar originals, processed into a static PNG or animated WebP.

Only an image the owner explicitly uploads as avatar material becomes an asset;
chat photos and attachments never do. Processing checks the real format, size and
decodability, drops every metadata chunk by re-encoding, and centre-crops to a
square. Bytes live beside the database, never in events or backups; the row keeps
hashes and sizes only. A failed check stores nothing.
"""

import hashlib
import io
import os
import re
import uuid
import warnings
from datetime import datetime, timedelta
from pathlib import Path

from companion.contracts import CoreError
from companion.image_frames import MAX_DECODED_PIXELS, MAX_FRAMES

SIZE = 512
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 16_000_000
ANIMATED_SIZE = 256
MAX_ASSET_BYTES = 4 * 1024 * 1024
RETENTION_DAYS = 180
FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP", "image/gif": "GIF"}
ASSET_ID = re.compile(r"^avt_[a-f0-9]{32}$")


def directory(store):
    return Path(store.state_directory).resolve() / "profile-assets"


def _path(store, asset_id, *, animated=None):
    if not isinstance(asset_id, str) or not ASSET_ID.fullmatch(asset_id):
        raise CoreError("PROFILE_ASSET_UNAVAILABLE", 410)
    root = directory(store)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise CoreError("PROFILE_ASSET_UNAVAILABLE", 410)
    webp = root / (asset_id + ".webp")
    if animated is None:
        animated = webp.exists() or webp.is_symlink()
    target = webp if animated else root / (asset_id + ".png")
    if target.is_symlink():
        raise CoreError("PROFILE_ASSET_UNAVAILABLE", 410)
    return target


def _square(frame, size):
    from PIL import Image, ImageOps

    rgba = ImageOps.exif_transpose(frame).convert("RGBA")
    square = ImageOps.fit(rgba, (size, size), method=Image.Resampling.LANCZOS)
    square.info.clear()
    return square


def _animation(image):
    from PIL import ImageSequence

    size = min(ANIMATED_SIZE, *image.size)
    loop = image.info.get("loop", 1)
    if image.format == "GIF":
        # GIF stores repeats after the first play. WebP stores total plays.
        loop = 1 if "loop" not in image.info else loop + 1 if loop else 0
    frames, delays = [], []
    pixels = 0
    for frame in ImageSequence.Iterator(image):
        pixels += frame.width * frame.height
        if (len(frames) >= MAX_FRAMES or max(frame.size) > 4096
                or pixels > MAX_DECODED_PIXELS):
            raise ValueError("AVATAR_TOO_LARGE")
        frames.append(_square(frame, size))
        delays.append(frame.info.get("duration", 100))
    out = io.BytesIO()
    frames[0].save(out, format="WEBP", save_all=True, append_images=frames[1:],
                   duration=delays, loop=min(loop, 65535), method=3, quality=88)
    if out.tell() > MAX_ASSET_BYTES:
        raise ValueError("AVATAR_TOO_LARGE")
    return out.getvalue(), size, size


def process(data, mime):
    """Validate and re-encode pixels, retaining the timeline of GIF and WebP avatars."""
    from PIL import Image

    if mime not in FORMATS:
        raise ValueError("AVATAR_FORMAT_UNSUPPORTED")
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("AVATAR_TOO_LARGE" if data else "AVATAR_EMPTY")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as decoded:
                if (decoded.format != FORMATS[mime] or min(decoded.size) < 1
                        or max(decoded.size) > 4096 or decoded.width * decoded.height > MAX_PIXELS):
                    raise ValueError("AVATAR_IMAGE_INVALID")
                if getattr(decoded, "is_animated", False):
                    if decoded.format not in ("GIF", "WEBP"):
                        raise ValueError("AVATAR_FORMAT_UNSUPPORTED")
                    return _animation(decoded)
                decoded.verify()
            with Image.open(io.BytesIO(data)) as decoded:
                square = _square(decoded, SIZE)
    except (EOFError, OSError, SyntaxError, Warning, Image.DecompressionBombError) as error:
        raise ValueError("AVATAR_IMAGE_INVALID") from error
    out = io.BytesIO()
    square.save(out, format="PNG", optimize=True)
    return out.getvalue(), SIZE, SIZE


def content_type(data):
    return "image/webp" if data.startswith(b"RIFF") and data[8:12] == b"WEBP" else "image/png"


def _write(store, asset_id, data):
    target = _path(store, asset_id, animated=content_type(data) == "image/webp")
    target.parent.mkdir(mode=0o700, exist_ok=True)
    partial = target.with_suffix(".tmp")
    with open(partial, "xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(partial, target)


def create(store, now, *, data, mime, operation_id):
    """Process and register one upload; the same operation returns the same asset."""
    if not store.in_write_transaction:
        raise RuntimeError("AVATAR_WRITE_TRANSACTION_REQUIRED")
    previous = store.one("SELECT * FROM companion_avatar_assets WHERE origin_operation_id=%s",
                         (operation_id,))
    if previous:
        return dict(previous)
    try:
        encoded, width, height = process(data, mime)
    except ValueError as error:
        raise CoreError(str(error), 400) from error
    asset_id = "avt_" + uuid.uuid4().hex
    _write(store, asset_id, encoded)
    store.execute(
        "INSERT INTO companion_avatar_assets (asset_id,sha256,original_sha256,source,state,"
        "width,height,byte_length,origin_operation_id,created_at) VALUES "
        "(%s,%s,%s,'owner_upload','READY',%s,%s,%s,%s,%s)",
        (asset_id, hashlib.sha256(encoded).hexdigest(), hashlib.sha256(data).hexdigest(),
         width, height, len(encoded), operation_id, now))
    return dict(store.one("SELECT * FROM companion_avatar_assets WHERE asset_id=%s",
                          (asset_id,)))


def ready(store, asset_id):
    row = store.one("SELECT * FROM companion_avatar_assets WHERE asset_id=%s AND state='READY'",
                    (asset_id,))
    return dict(row) if row else None


def available(store, limit=12):
    return [dict(row) for row in store.rows(
        "SELECT asset_id,width,height,created_at FROM companion_avatar_assets "
        "WHERE state='READY' ORDER BY created_at DESC LIMIT %s", (limit,))]


def read_bytes(store, asset_id):
    row = ready(store, asset_id)
    if row is None:
        raise CoreError("PROFILE_ASSET_UNAVAILABLE", 410)
    try:
        data = _path(store, asset_id).read_bytes()
    except OSError as error:
        raise CoreError("PROFILE_ASSET_UNAVAILABLE", 410) from error
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise CoreError("PROFILE_ASSET_UNAVAILABLE", 410)
    return data


def cleanup(store, now, *, keep_days=RETENTION_DAYS):
    """Erase unused assets older than the published retention; records stay readable."""
    cutoff = (datetime.fromisoformat(now) - timedelta(days=keep_days)).isoformat(
        timespec="microseconds")
    current = store.one("SELECT avatar_asset_id FROM companion_profile WHERE singleton='self'")
    keep = current["avatar_asset_id"] if current else None
    removed = 0
    for row in store.rows("SELECT asset_id FROM companion_avatar_assets WHERE state='READY' "
                          "AND created_at < %s", (cutoff,)):
        if row["asset_id"] == keep:
            continue
        try:
            _path(store, row["asset_id"]).unlink(missing_ok=True)
        except CoreError:
            pass
        store.execute("UPDATE companion_avatar_assets SET state='ERASED', erased_at=%s "
                      "WHERE asset_id=%s AND state='READY'", (now, row["asset_id"]))
        removed += 1
    return removed
