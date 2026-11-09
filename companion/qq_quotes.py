"""Local status of QQ reply quotes: erased, forgotten or recalled sources stay withdrawn.

The Bridge reads a quoted message from QQ, which does not know about Core erasure or
owner recall. The signed capture is left unchanged so replay still verifies; only what
reaches the model is withdrawn. Lookups stay in the current conversation and protocol
epoch namespace, and a reference that matches no local row is left as the Bridge read it.
"""

import re

from companion.input_recall import is_recalled

PLACEHOLDER = {"status": "WITHDRAWN", "note": "原文已被主人删除或撤回，看不到内容"}


PRIVATE = re.compile(
    r"[1-9][0-9]{4,19}:onebot-direct:FriendMessage:[1-9][0-9]{4,19}:epoch:[A-Za-z0-9_-]{8,64}")


def namespace(source_event_id):
    """The `<bot>:<origin>:epoch:<E>` prefix of a private QQ input, or None."""
    prefix, _, provider_id = (source_event_id or "").rpartition(":")
    if not PRIVATE.fullmatch(prefix) or not re.fullmatch(r"-?[1-9][0-9]{0,19}", provider_id):
        return None
    return prefix


def withdrawn(store, event_id, body):
    """Return indices of reply segments whose quoted local message must not be shown."""
    segments = [s for s in (body.get("rich") or {}).get("segments", [])
                if s.get("type") == "reply" and isinstance(s.get("reference"), str)]
    if not segments:
        return frozenset()
    current = store.one('SELECT conversation_id, source_event_id FROM events WHERE event_id = %s',
                        (event_id,))
    prefix = namespace(current and current["source_event_id"])
    if not prefix:
        return frozenset()
    result = set()
    for segment in segments:
        reference = segment["reference"]
        source_id = f"{prefix}:{reference}"
        owner = store.one(
            'SELECT redacted FROM events WHERE source_event_id = %s AND conversation_id = %s',
            (source_id, current["conversation_id"]))
        sent = store.rows(
            'SELECT e.redacted FROM outbox AS o JOIN events AS e ON e.event_id = o.origin_event_id '
            'WHERE o.conversation_id = %s AND o.provider_message_id = %s LIMIT 2',
            (current["conversation_id"], reference))
        if (is_recalled(store, source_id) or (owner and owner["redacted"])
                or any(row["redacted"] for row in sent)):
            result.add(segment["index"])
    return frozenset(result)


def media_slots(body, indices):
    """Image slots that belong to withdrawn reply segments."""
    if not indices:
        return frozenset()
    return frozenset(
        item["slot"]
        for segment in (body.get("rich") or {}).get("segments", [])
        if segment.get("index") in indices
        for item in segment.get("body", {}).get("media", [])
        if isinstance(item.get("slot"), int))
