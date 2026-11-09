"""Narrow native NOT_SENT receipts; manual uncertain-send decisions stay separate."""

from companion_delivery_protocol import (
    DELIVERY_FIELDS,
    MAX_RECEIPT_BYTES,
    canonical,
    digest,
    hash_value,
    identifier,
    timestamp,
    validate_quiescence,
)

KIND = "owner_delivery_not_sent_v1"
TEXT_ACTIONS = frozenset({"qq.private.reply", "qq.private.proactive"})


def proof(value):
    value = validate_quiescence(value)
    if (value["version"] != 2 or value["action_type"] not in TEXT_ACTIONS
        or value["attempt_state"] != "RESERVED" or value["original_result_sha256"] is not None
        or value["attempt_record_sha256"] != value["reserved_record_sha256"]
        or value["content_sha256"] is None or "voice_check" in value):
        raise ValueError("DELIVERY_NOT_SENT_PROOF_REQUIRED")
    return value


def delivery(value):
    if (not isinstance(value, dict) or set(value) != DELIVERY_FIELDS
        or value["action_type"] not in TEXT_ACTIONS
        or not identifier(value["message_id"], r"msg_[a-f0-9]{32}")
        or not identifier(value["event_id"], r"[A-Za-z0-9_:-]{8,128}")
        or type(value["attempt_count"]) is not int or value["attempt_count"] != 0
        or value["original_state"] != "CANCELLED"
        or any(not hash_value(value[key]) for key in DELIVERY_FIELDS if key.endswith("_sha256"))):
        raise ValueError("DELIVERY_NOT_SENT_BINDING_INVALID")
    timestamp(value["created_at"])
    if value["lease_until"] is not None:
        timestamp(value["lease_until"])
    return dict(value)


def receipt(value):
    if (not isinstance(value, dict) or set(value) != {
        "kind", "version", "state", "paused", "at", "delivery", "quiescence", "receipt_sha256",
    } or value["kind"] != KIND or type(value["version"]) is not int or value["version"] != 1
        or value["state"] != "NOT_SENT" or value["paused"] is not True):
        raise ValueError("DELIVERY_NOT_SENT_RECEIPT_INVALID")
    source, original = proof(value["quiescence"]), delivery(value["delivery"])
    if (timestamp(value["at"]) < timestamp(source["quiesced_at"])
        or any(original[key] != source[key] for key in (
            "message_id", "event_id", "action_type", "content_sha256"))
        or value["receipt_sha256"] != digest({key: item for key, item in value.items()
                                             if key != "receipt_sha256"})
        or len(canonical(value)) > MAX_RECEIPT_BYTES):
        raise ValueError("DELIVERY_NOT_SENT_RECEIPT_INVALID")
    return dict(value)
