"""Authenticated evidence for an expired, pre-dispatch owner text delivery."""

from companion_delivery_protocol import canonical

BUSY = "DELIVERY_PREFLIGHT_DATABASE_BUSY"
KIND = "owner_reply_preflight_expired_v1"


def eligible(item):
    return ("authority" in item
            and (item.get("action_type") == "qq.private.proactive"
                 or item.get("action_type") == "qq.private.reply" and "reply_revision" in item)
            and "voice" not in item
            and not ({"image", "generated_image"} & item["content"].keys()))


def signing_bytes(proof):
    return KIND.encode() + b"\0" + canonical({k: v for k, v in proof.items() if k != "signature"})
