"""Native device identities over the existing owner capture signature contract."""

import hashlib
import re

from companion_delivery_protocol import canonical

SOURCE = "native.chat"
SESSION = "native:owner"
KIND = "authenticated_native_device"
EPOCH = "native-v1"


def device_identity(public_key, *, protocol_epoch=EPOCH):
    if not isinstance(public_key, str) or not re.fullmatch(r"[a-f0-9]{64}", public_key):
        raise ValueError("NATIVE_DEVICE_PUBLIC_KEY_REQUIRED")
    device_id = hashlib.sha256(bytes.fromhex(public_key)).hexdigest()[:32]
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', protocol_epoch):
        raise ValueError('NATIVE_DEVICE_EPOCH_REQUIRED')
    return device_id, {
        "public_key": public_key,
        "identity_sha256": hashlib.sha256(canonical(
            [SOURCE, device_id, public_key] if protocol_epoch == EPOCH
            else [SOURCE, device_id, public_key, protocol_epoch])).hexdigest(),
        "protocol_epoch": protocol_epoch,
    }


def source_identity(source_event_id):
    match = re.fullmatch(r"native:([a-f0-9]{32}):([a-f0-9]{32})", source_event_id)
    if match is None:
        raise ValueError("NATIVE_SOURCE_IDENTITY_REQUIRED")
    return match.groups()


def capture(public_key, authority, request_id, text, occurred_at, *, protocol_epoch=EPOCH):
    device_id, identity = device_identity(public_key, protocol_epoch=protocol_epoch)
    source = f"native:{device_id}:{request_id}"
    source_identity(source)
    text_sha256 = hashlib.sha256(canonical(text)).hexdigest()
    return {
        "authority": authority, "kind": KIND,
        "event_id": "evt_" + hashlib.sha256(source.encode()).hexdigest(),
        "source_event_id": source, "input_sha256": text_sha256,
        "frame_sha256": hashlib.sha256(canonical([request_id, text, occurred_at])).hexdigest(),
        "evidence_sha256": hashlib.sha256(canonical([SOURCE, device_id, request_id])).hexdigest(),
        "identity_sha256": identity["identity_sha256"], "protocol_epoch": protocol_epoch,
        "received_at": occurred_at, "occurred_at": occurred_at,
    }
