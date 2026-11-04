"""Bounded HTTP reply-fragment contract; independent of the Core package."""

import hashlib
import re

from companion_delivery_protocol.reply_groups import manifest, mixed_part, part_hash

from .spool import encode

FIELDS = {"version", "group_id", "index", "count", "group_sha256", "total_chars",
          "interval_ms", "part_sha256s"}


def wire_text(item):
    """Omit outer blank lines in a bubble; retain the lossless source for its receipt."""
    text = item["content"]["text"]
    if envelope(item) is None:
        return text
    lines = text.splitlines(keepends=True)
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "".join(lines[start:end]).rstrip("\r\n")


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def quote(item):
    """The OneBot reply segment Core resolved for a native owner reply, or none.

    Only the first bubble of an owner-authority reply may quote; the id is the
    provider message id Core recorded itself, never text chosen by the model.
    """
    target = item.get("content", {}).get("reply_to")
    if target is None:
        return []
    segment = item.get("reply_segment")
    if ("authority" not in item or item.get("action_type") != "qq.private.reply"
        or "voice" in item or not isinstance(target, dict) or set(target) != {"message_id"}
        or not isinstance(target["message_id"], str)
        or not re.fullmatch(r"-?[1-9][0-9]{0,19}", target["message_id"])
        or (segment is not None and (not isinstance(segment, dict)
                                     or segment.get("index") != 0))):
        raise ValueError("QUOTE_SCOPE_REQUIRED")
    return [{"type": "reply", "data": {"id": target["message_id"]}}]


def scope(plan):
    return plan.value.get("live_text", {}).get("segmented_reply")


def validate_scope(value, outer):
    policy = value.get("segmented_reply")
    if (not isinstance(policy, dict)
        or set(policy) not in ({"max_segments", "min_chars", "interval_ms"},
                               {"max_segments", "min_chars", "interval_ms", "mode"})
        or policy.get("mode", "sentence") not in {"sentence", "semantic"}
        or (policy.get("max_segments") != "unlimited" and (
            type(policy.get("max_segments")) is not int
            or not 2 <= policy["max_segments"] <= 2**31 - 1))
        or any(type(policy[name]) is not int or not low <= policy[name] <= high
               for name, low, high in (("min_chars", 4, 100),
                                       ("interval_ms", 0, 3000)))
        or type(value.get("max_send_attempts")) is not int
        or not 1 <= value["max_send_attempts"] <= 2**31 - 1
        or value["max_send_attempts"] != min(outer["max_attempts"], outer["max_visible_replies"])):
        raise ValueError("INVALID_SEGMENTED_REPLY_SCOPE")


def event_id(item):
    if "authority" in item:
        from companion_delivery_protocol.authority import validate_authority_ref

        validate_authority_ref(item["authority"])
        value = item.get("origin_event_id")
        if ("window_origin_event_id" in item or not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9_:-]{8,128}", value)):
            raise ValueError("INVALID_OWNER_ORIGIN")
        return value
    if "window_origin_event_id" in item:
        value = item["window_origin_event_id"]
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9_:-]{8,128}", value,
        ):
            raise ValueError("INVALID_WINDOW_ORIGIN")
        return value
    key = item.get("delivery_key", "")
    match = (re.fullmatch(r"reply:(evt_[a-f0-9]{64}):v(?:1|2:(?:0|[1-9][0-9]{0,9}))", key)
             if isinstance(key, str) else None)
    if not match:
        raise ValueError("WINDOW_REPLY_KEY_REQUIRED")
    return match[1]


def envelope(item):
    """Validate the ordered text group or its native content-bound image-tail variant."""
    key = item.get("delivery_key", "")
    present = "reply_segment" in item
    v2 = isinstance(key, str) and ":v2" in key
    if not present and not v2:
        return None
    value = item.get("reply_segment")
    match = (re.fullmatch(r"reply:(evt_[a-f0-9]{64}):v2:(0|[1-9][0-9]{0,9})", key)
             if isinstance(key, str) else None)
    if (not match or not isinstance(value, dict) or set(value) != FIELDS
        or type(value["version"]) is not int or value["version"] not in (1, 2, 3)
        or any(type(value[name]) is not int for name in
               ("index", "count", "total_chars", "interval_ms"))
        or not 1 <= value["count"] <= 2**31 - 1 or not 0 <= value["index"] < value["count"]
        or value["index"] != int(match[2]) or not 0 <= value["interval_ms"] <= 3000
        or not isinstance(value["group_id"], str)
        or not re.fullmatch(r"job_[a-f0-9]{32}", value["group_id"])
        or not isinstance(value["part_sha256s"], list)
        or len(value["part_sha256s"]) != value["count"]
        or any(not isinstance(part, str) or not re.fullmatch(r"[a-f0-9]{64}", part)
               for part in value["part_sha256s"])):
        raise ValueError("INVALID_REPLY_SEGMENT")
    text = item["content"]["text"]
    version = value["version"]
    if version == 3:
        choice = mixed_part(item["content"])
        if ("authority" not in item or item.get("action_type") != "qq.private.reply"
            or "reply_to" in item["content"] and value["index"] != 0
            or "voice" in item and choice["kind"] not in {"voice", "singing"}
            or choice["kind"] == "singing" and "voice" not in item):
            raise ValueError("INVALID_REPLY_SEGMENT")
    if version == 2 and (
        "authority" not in item or item.get("action_type") != "qq.private.reply"
        or value["count"] < 2
        or (value["index"] == value["count"] - 1 and not image_only(item))
        or (value["index"] < value["count"] - 1 and (
            not text.strip() or set(item["content"]) - (
                {"reply_to"} if value["index"] == 0 else set()) != {"text"}))
    ):
        raise ValueError("INVALID_REPLY_SEGMENT")
    if (not len(text) <= value["total_chars"] <= 20000
        or part_hash(item["content"], version) != value["part_sha256s"][value["index"]]
        or value["group_sha256"] != digest(manifest(value["group_id"], value["part_sha256s"],
            value["total_chars"], value["interval_ms"], version))):
        raise ValueError("INVALID_REPLY_SEGMENT")
    return value


def image_only(item):
    """The image tail is native-owner-only and must still pass full envelope validation."""
    value, content = item.get("reply_segment"), item.get("content")
    if (isinstance(value, dict) and value.get("version") == 3
        and isinstance(content, dict) and content.get("text") == ""):
        try:
            choice = mixed_part(content)
        except ValueError:
            return False
        return ("authority" in item and item.get("action_type") == "qq.private.reply"
                and choice["kind"] in {"meme", "generated_image"})
    return ("authority" in item and item.get("action_type") == "qq.private.reply"
            and isinstance(value, dict) and value.get("version") == 2
            and type(value.get("index")) is int and type(value.get("count")) is int
            and isinstance(content, dict) and set(content) == {"text", "image"}
            and content["text"] == "" and isinstance(content["image"], dict)
            and value.get("index") == value.get("count", 0) - 1)


def voice_content(item):
    content = item.get("content", {})
    if set(content) == {"text"}:
        return True
    try:
        return (envelope(item) is not None and item["reply_segment"]["version"] == 3
                and mixed_part(content)["kind"] in {"voice", "singing"})
    except (KeyError, TypeError, ValueError):
        return False


def journal_value(value, text):
    return {key: value[key] for key in FIELDS - {"group_id"}} | {
        "group_ref_sha256": digest(value["group_id"]), "chars": len(text),
    }
