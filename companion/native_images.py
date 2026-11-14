"""Images the owner attaches in the first-party native client.

The device signs each image's SHA-256 inside the message's rich projection, so the
bytes submitted with the message are bound to the owner's signature; no separate
upload lease or Bridge proof exists. Accepted originals enter the ordinary owner
image intake (short-lived spool, model input). A small re-encoded preview is kept
for the chat history and is deleted with the message.
"""

import base64
import binascii
import hashlib
import io
import os
import re
import warnings
from pathlib import Path
from typing import Literal

from pydantic import Field

from companion.contracts import CoreError, StrictModel

MAX_IMAGES = 4
MAX_TOTAL_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 40_000_000
PREVIEW_SIDE = 960
FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP", "image/gif": "GIF"}
# Base64 of every original plus JSON overhead; the route enforces it before parsing.
SUBMIT_BODY_BYTES = (MAX_TOTAL_BYTES + 2) // 3 * 4 + 256 * 1024


class NativeImage(StrictModel):
    mime_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    data_base64: str = Field(min_length=4, max_length=(MAX_TOTAL_BYTES + 2) // 3 * 4)


def decode(settings, images):
    """Exact bytes, MIME and SHA-256 per slot; every image must really be its claimed type."""
    if len(images) > MAX_IMAGES:
        raise CoreError("NATIVE_IMAGE_COUNT_EXCEEDED", 400)
    limit = min(settings.content.max_image_bytes, MAX_TOTAL_BYTES)
    decoded, total = [], 0
    for image in images:
        try:
            data = base64.b64decode(image.data_base64, validate=True)
        except (binascii.Error, ValueError):
            raise CoreError("NATIVE_IMAGE_INVALID", 400) from None
        total += len(data)
        if not data or len(data) > limit or total > MAX_TOTAL_BYTES:
            raise CoreError("NATIVE_IMAGE_TOO_LARGE", 413)
        _check(data, image.mime_type)
        decoded.append((image.mime_type, data, hashlib.sha256(data).hexdigest()))
    return decoded


def _check(data, mime):
    from PIL import Image

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if (image.format != FORMATS[mime]
                        or image.width * image.height > MAX_PIXELS):
                    raise ValueError("format")
                image.verify()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Warning):
        raise CoreError("NATIVE_IMAGE_INVALID", 400) from None


def directory(store):
    return Path(store.state_directory).resolve() / "native-images"


def _path(store, event_id, slot, extension):
    if not re.fullmatch(r"evt_[a-f0-9]{64}", event_id) or not 0 <= slot < MAX_IMAGES:
        raise CoreError("NATIVE_IMAGE_NOT_FOUND", 404)
    return directory(store) / f"{event_id}.{slot}.{extension}"


def save_preview(store, event_id, slot, data):
    """A metadata-free re-encode for history display; the original is never kept here."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as source:
        source.seek(0)
        image = ImageOps.exif_transpose(source)
        alpha = image.mode in {"RGBA", "LA"} or (
            image.mode == "P" and "transparency" in image.info)
        image = image.convert("RGBA" if alpha else "RGB")
        image.thumbnail((PREVIEW_SIDE, PREVIEW_SIDE))
        out = io.BytesIO()
        if alpha:
            image.save(out, "PNG", optimize=True)
        else:
            image.save(out, "JPEG", quality=85, optimize=True)
    root = directory(store)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = _path(store, event_id, slot, "png" if alpha else "jpg")
    temporary = target.with_suffix(target.suffix + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(out.getvalue())
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def read_preview(store, event_id, slot):
    for extension, mime in (("jpg", "image/jpeg"), ("png", "image/png")):
        target = _path(store, event_id, slot, extension)
        if target.is_file():
            return target.read_bytes(), mime
    raise CoreError("NATIVE_IMAGE_NOT_FOUND", 404)


def erase(store, event_ids):
    """Called with every erased event; removes any history previews it owned."""
    for event_id in event_ids:
        if not re.fullmatch(r"evt_[a-f0-9]{64}", event_id):
            continue
        for slot in range(MAX_IMAGES):
            for extension in ("jpg", "png"):
                try:
                    _path(store, event_id, slot, extension).unlink()
                except FileNotFoundError:
                    pass


def image_rich(text, image_sha256):
    """The signed projection of a native message with attached images, or None.

    Text (when present) is position 0; each image is one UNREAD image segment whose
    source_sha256 is the SHA-256 of the exact bytes the device sends. The client
    builds the same value, so the existing rich input digest covers the images.
    """
    if not image_sha256:
        return None
    offset = 1 if text.strip() else 0
    return {
        "version": "references_v1", "coverage": "PARTIAL",
        "segment_count": offset + len(image_sha256),
        "owner_text_positions": [0] if offset else [],
        "segments": [{"index": offset + slot, "type": "image", "actor": "UNVERIFIED",
                      "coverage": "UNREAD", "slot": slot, "source_sha256": sha}
                     for slot, sha in enumerate(image_sha256)],
    }


def input_sha256(text, image_sha256=()):
    """What a native capture signs: the bare text, or text plus the image projection."""
    from companion.qq_rich import input_digest

    rich = image_rich(text, list(image_sha256))
    return input_digest({"text": text, **({"rich": rich} if rich else {})})
