"""Strict Reader v1 lease decoding without importing Core or storage dependencies."""

import re
from datetime import UTC, datetime

from companion.source_policy import public_web_host
from source_reader.network import ReadFailure
from source_reader.reading import ReadingContext


def instant(value):
    if not isinstance(value, str):
        raise ValueError("Expected an aware timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Expected an aware timestamp")
    return parsed.astimezone(UTC)


def exact_hosts(values):
    if isinstance(values, (list, tuple)) and tuple(values) == ("*",):
        return frozenset(values)
    if (not isinstance(values, (list, tuple)) or len(values) > 128
        or any(not isinstance(host, str) or len(host) > 253 or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", host,
        ) for host in values) or len(set(values)) != len(values)):
        raise ValueError("Use a bounded list of exact lowercase hostnames")
    return frozenset(values)


def decode_lease(payload, source, now, *, media_limits=None):
    """Reject partial/foreign contracts before any provider or browser operation."""
    try:
        if not isinstance(payload, dict) or set(payload) != {"item"}:
            raise ValueError("Invalid lease response")
        item = payload["item"]
        if item is None:
            return None
        required = {
            "schema_version", "request_id", "lease_token", "lease_until", "operation", "url",
            "max_bytes", "max_requests", "max_items", "context", "allowed_hosts",
        }
        if (not isinstance(item, dict) or not required <= set(item)
            or set(item) - required - {"source_grant", "media", "query", "discovery_mode"}
            or type(item["schema_version"]) is not int or item["schema_version"] != 1):
            raise ValueError("Invalid lease fields")
        for key, minimum in (("request_id", 1), ("lease_token", 8)):
            value = item[key]
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{" + str(minimum) + r",128}", value,
            ):
                raise ValueError("Invalid lease identity")
        media = item.get("media")
        maximum_bytes = 1048576
        if media is not None:
            from companion.bili_video_protocol import MAX_DEADLINE_SECONDS, media_from_wire

            if media_limits is None or source != "bilibili":
                raise ValueError("media requires explicit Bili Reader capability")
            media_from_wire(media, local_limits=media_limits)
            maximum_bytes += media_limits.max_input_bytes
        for name, maximum in (("max_bytes", maximum_bytes), ("max_requests", 8), ("max_items", 40)):
            if type(item[name]) is not int or not 1 <= item[name] <= maximum:
                raise ValueError("Invalid lease limit")
        if (item["operation"] not in {"read", "discover"}
            or not isinstance(item["url"], str) or len(item["url"]) > 2048
            or item["operation"] == "read" and not item["url"]):
            raise ValueError("Invalid operation")
        if "query" in item and (
            item["operation"] != "discover" or item["url"]
            or not isinstance(item["query"], str) or not 2 <= len(item["query"]) <= 100
            or not item["query"].strip() or re.search(r"[\x00-\x1f\x7f]", item["query"])
        ):
            raise ValueError("Invalid discover query")
        if "discovery_mode" in item and (
            item["discovery_mode"] != "recommend" or source != "bilibili"
            or item["operation"] != "discover" or item["url"] or "query" in item
            or not item.get("source_grant") or item["max_requests"] < 2
        ):
            raise ValueError("Invalid account discovery")
        context = item["context"]
        if source == "qzone" and (item["operation"] != "read"
                                  or item["max_items"] != 1 or not item.get("source_grant")):
            raise ValueError("QZone requires one account-bound target")
        if (not isinstance(context, dict)
            or set(context) != {"root_task_id", "source", "source_event_ids", "privacy_epoch"}
            or not isinstance(context["source_event_ids"], list) or context["source"] != source):
            raise ValueError("Invalid context")
        bound = ReadingContext(
            **{**context, "source_event_ids": tuple(context["source_event_ids"])}
        )
        hosts = exact_hosts(item["allowed_hosts"])
        if "*" in hosts and source != "web":
            raise ValueError("Only web reads can permit all public DNS names")
        if source == "web" and any(host != "*" and not public_web_host(host) for host in hosts):
            raise ValueError("Web reads require public DNS names")
        if source == "web" and (not hosts or item["operation"] != "read"):
            raise ValueError("Web reads require an explicit host scope")
        if item.get("source_grant") is not None:
            grant = item["source_grant"]
            if (not isinstance(grant, dict)
                or set(grant) != {"grant_id", "epoch", "fingerprint", "expires_at"}
                or not isinstance(grant["grant_id"], str)
                or not re.fullmatch(r"grant_[0-9a-f]{32}", grant["grant_id"])
                or type(grant["epoch"]) is not int or not 1 <= grant["epoch"] <= 2147483647
                or not isinstance(grant["fingerprint"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", grant["fingerprint"])):
                raise ValueError("Invalid grant snapshot")
            instant(grant["expires_at"])
        until = instant(item["lease_until"])
        if media is not None and (
            item["operation"] != "read" or item["max_items"] != 1
            or item["url"] != "https://www.bilibili.com/video/" + media["bvid"]
            or media["source_event_id"] not in context["source_event_ids"]
            or instant(media["deadline"]) != until
            or (until - now).total_seconds() > MAX_DEADLINE_SECONDS
            or item.get("source_grant") and instant(item["source_grant"]["expires_at"]) < until
        ):
            raise ValueError("media source or deadline differs from its lease")
    except (TypeError, ValueError, KeyError, OverflowError):
        raise ReadFailure("READER_LEASE_INVALID") from None
    if until <= now:
        raise ReadFailure("STALE_READER_LEASE")
    return item, bound, hosts, until
