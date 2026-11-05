"""Dependency-free owner authority reference for the Core/Bridge HTTP v1 boundary."""

import re
from dataclasses import asdict, dataclass
from datetime import datetime

MAX_OWNER_IMAGE_BYTES = 32 * 1024 * 1024

CAPTURE_DOMAIN = b"companion-core/owner-capture/v2\x00"
DELIVERY_DOMAIN = b"companion-core/owner-delivery/v2\x00"
QUIESCENCE_DOMAIN = b"companion-core/owner-quiescence/v2\x00"
RECALL_DOMAIN = b"companion-core/owner-recall/v2\x00"
FIELDS = {"version", "grant_id", "revision", "policy_sha256", "restore_generation"}


@dataclass(frozen=True)
class AuthorityRef:
    version: int
    grant_id: str
    revision: int
    policy_sha256: str
    restore_generation: str

    def __post_init__(self):
        if (type(self.version) is not int or self.version != 2
            or type(self.revision) is not int or not 1 <= self.revision <= 2**53 - 1
            or not isinstance(self.grant_id, str)
            or not re.fullmatch(r"grant_[a-f0-9]{32}", self.grant_id)
            or not isinstance(self.policy_sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", self.policy_sha256)
            or not isinstance(self.restore_generation, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.restore_generation)):
            raise ValueError("OWNER_AUTHORITY_REFERENCE_INVALID")

    @classmethod
    def from_dict(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict) or set(value) != FIELDS:
            raise ValueError("OWNER_AUTHORITY_REFERENCE_INVALID")
        return cls(**value)

    def as_dict(self):
        return asdict(self)


def validate_authority_ref(value):
    return AuthorityRef.from_dict(value).as_dict()


# A live WebSocket frame, or an owner message read back from the protocol's history
# after a Bridge restart left the socket closed for a few seconds.
CAPTURE_KINDS = frozenset({"authenticated_onebot_ws", "onebot_history_backfill"})


def capture_signing_payload(value):
    """Canonical v2 capture bytes shared by the native Bridge and original-proof reader."""
    from companion_delivery_protocol import canonical, hash_value, identifier

    fields = {"authority", "kind", "event_id", "source_event_id", "input_sha256",
              "frame_sha256", "evidence_sha256", "identity_sha256", "protocol_epoch",
              "received_at", "occurred_at"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("OWNER_CAPTURE_INVALID")
    result = {**value, "authority": validate_authority_ref(value["authority"])}
    if (result["kind"] not in CAPTURE_KINDS
        or not identifier(result["event_id"], r"evt_[a-f0-9]{64}")
        or not isinstance(result["source_event_id"], str)
        or not 1 <= len(result["source_event_id"]) <= 512
        or not identifier(result["protocol_epoch"], r"[A-Za-z0-9_-]{8,64}")
        or any(not hash_value(result[key]) for key in fields if key.endswith("_sha256"))):
        raise ValueError("OWNER_CAPTURE_INVALID")
    for key in ("received_at", "occurred_at"):
        if not isinstance(result[key], str) or len(result[key]) > 48:
            raise ValueError("OWNER_CAPTURE_INVALID")
        parsed = datetime.fromisoformat(result[key])
        if parsed.utcoffset() is None:
            raise ValueError("OWNER_CAPTURE_INVALID")
        result[key] = parsed.isoformat()
    return CAPTURE_DOMAIN + canonical(result)


def validate_send_check(value):
    """Shape only: authority, custody, source and finite lease remain Core checks."""
    from companion_delivery_protocol import hash_value, identifier, validate_voice_check

    fields = {"authority", "event_id", "message_id", "content_sha256", "lease_token"}
    if (not isinstance(value, dict) or not fields <= set(value)
        or set(value) - fields - {"reply_revision", "voice"}
        or not identifier(value["event_id"], r"[A-Za-z0-9_:-]{8,128}")
        or not identifier(value["message_id"], r"msg_[a-f0-9]{32}")
        or not identifier(value["lease_token"], r"lease_[a-f0-9]{32}")
        or not hash_value(value["content_sha256"])
        or "reply_revision" in value and (
            type(value["reply_revision"]) is not int or not 0 <= value["reply_revision"] < 2**53)):
        raise ValueError("OWNER_SEND_CHECK_INVALID")
    result = {**value, "authority": validate_authority_ref(value["authority"])}
    if "voice" in value:
        result["voice"] = validate_voice_check(value["voice"])
    return result


def recall_signing_payload(value):
    """Versioned canonical recall bytes; the original capture proves direction."""
    from companion_delivery_protocol import canonical, hash_value, identifier

    fields = {"authority", "kind", "recall_id", "target_source_event_id", "target_event_id",
              "protocol_epoch", "identity_sha256", "occurred_at", "received_at",
              "frame_sha256", "evidence_sha256"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("OWNER_RECALL_INVALID")
    result = {**value, "authority": validate_authority_ref(value["authority"])}
    if (result["kind"] != "authenticated_onebot_friend_recall"
        or not identifier(result["recall_id"], r"recall_[a-f0-9]{64}")
        or not identifier(result["target_event_id"], r"evt_[a-f0-9]{64}")
        or not isinstance(result["target_source_event_id"], str)
        or not 1 <= len(result["target_source_event_id"]) <= 512
        or not identifier(result["protocol_epoch"], r"[A-Za-z0-9_-]{8,64}")
        or any(not hash_value(result[key]) for key in fields if key.endswith("_sha256"))):
        raise ValueError("OWNER_RECALL_INVALID")
    for key in ("received_at", "occurred_at"):
        if not isinstance(result[key], str) or len(result[key]) > 48:
            raise ValueError("OWNER_RECALL_INVALID")
        parsed = datetime.fromisoformat(result[key])
        if parsed.utcoffset() is None:
            raise ValueError("OWNER_RECALL_INVALID")
        result[key] = parsed.isoformat()
    return RECALL_DOMAIN + canonical(result)


IMAGE_UPLOAD_DOMAIN = b"companion-core/owner-image/v2\x00"


def image_upload_signing_payload(value):
    """Bind original pixels to an admitted source, slot and finite Core upload lease."""
    from companion_delivery_protocol import canonical, hash_value, identifier

    fields = {"authority", "event_id", "slot", "source_sha256", "lease_token",
              "media_sha256", "mime"}
    if (not isinstance(value, dict) or set(value) != fields
        or not identifier(value["event_id"], r"evt_[a-f0-9]{64}")
        or type(value["slot"]) is not int or not 0 <= value["slot"] < 64
        or not identifier(value["lease_token"], r"content_lease_[a-f0-9]{32}")
        or not hash_value(value["source_sha256"]) or not hash_value(value["media_sha256"])
        or value["mime"] not in {
            "image/png", "image/jpeg", "image/webp", "image/gif", "image/apng"}):
        raise ValueError("OWNER_IMAGE_UPLOAD_INVALID")
    return IMAGE_UPLOAD_DOMAIN + canonical({
        **value, "authority": validate_authority_ref(value["authority"]),
    })


FILE_UPLOAD_DOMAIN = b"companion-core/owner-file/v2\x00"


def file_upload_signing_payload(value):
    """Bind original file bytes to their signed source and finite upload lease."""
    from companion_delivery_protocol import canonical, hash_value, identifier

    fields = {"authority", "event_id", "slot", "source_sha256", "lease_token",
              "file_id", "filename", "media_sha256", "mime", "bytes"}
    if (not isinstance(value, dict) or set(value) != fields
        or not identifier(value["event_id"], r"evt_[a-f0-9]{64}")
        or type(value["slot"]) is not int or not 0 <= value["slot"] < 64
        or not identifier(value["lease_token"], r"content_lease_[a-f0-9]{32}")
        or not hash_value(value["source_sha256"]) or not hash_value(value["media_sha256"])
        or type(value["bytes"]) is not int or not 1 <= value["bytes"] <= 2**53 - 1
        or any(not isinstance(value[key], str) or not 1 <= len(value[key]) <= maximum
               or re.search(r"[\x00-\x1f\x7f]", value[key])
               for key, maximum in (("file_id", 512), ("filename", 255), ("mime", 256)))):
        raise ValueError("OWNER_FILE_UPLOAD_INVALID")
    return FILE_UPLOAD_DOMAIN + canonical({
        **value, "authority": validate_authority_ref(value["authority"]),
    })
