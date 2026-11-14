"""Native device identities over the existing owner capture signature contract."""

import hashlib
import re

from companion_delivery_protocol import canonical

SOURCE = "native.chat"
SESSION = "native:owner"
KIND = "authenticated_native_device"
EPOCH = "native-v1"


def device_identity(public_key):
    if not isinstance(public_key, str) or not re.fullmatch(r"[a-f0-9]{64}", public_key):
        raise ValueError("NATIVE_DEVICE_PUBLIC_KEY_REQUIRED")
    device_id = hashlib.sha256(bytes.fromhex(public_key)).hexdigest()[:32]
    return device_id, {
        "public_key": public_key,
        "identity_sha256": hashlib.sha256(canonical([SOURCE, device_id, public_key])).hexdigest(),
        "protocol_epoch": EPOCH,
    }


def source_identity(source_event_id):
    match = re.fullmatch(r"native:([a-f0-9]{32}):([a-f0-9]{32})", source_event_id)
    if match is None:
        raise ValueError("NATIVE_SOURCE_IDENTITY_REQUIRED")
    return match.groups()


def capture(public_key, authority, request_id, text, occurred_at, image_sha256=()):
    device_id, identity = device_identity(public_key)
    source = f"native:{device_id}:{request_id}"
    source_identity(source)
    text_sha256 = input_sha256(text, image_sha256)
    return {
        "authority": authority, "kind": KIND,
        "event_id": "evt_" + hashlib.sha256(source.encode()).hexdigest(),
        "source_event_id": source, "input_sha256": text_sha256,
        "frame_sha256": hashlib.sha256(canonical([request_id, text, occurred_at])).hexdigest(),
        "evidence_sha256": hashlib.sha256(canonical([SOURCE, device_id, request_id])).hexdigest(),
        "identity_sha256": identity["identity_sha256"], "protocol_epoch": EPOCH,
        "received_at": occurred_at, "occurred_at": occurred_at,
    }


def image_rich(text, image_sha256):
    """The signed projection of a native message with attached images, or None.

    Text (when present) is position 0; each image is one UNREAD image segment whose
    source_sha256 is the SHA-256 of the exact bytes the device sends.
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
    rich = image_rich(text, list(image_sha256))
    value = {"text": text, "rich": rich} if rich else text
    return hashlib.sha256(canonical(value)).hexdigest()
