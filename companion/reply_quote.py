"""Native QQ quote targets for owner-authority replies.

The model may only name a local alias of a message it was shown: an owner input or
one of its own delivered replies. Core maps the alias back to a record it kept itself
and decides whether a quote box is useful; an unknown, erased, recalled or
out-of-scope target drops the quote, never the reply.
"""

import hashlib
import json
import re

from companion import input_recall, reply_revision

CHOICE = re.compile(r"【引用\s*(m[0-9a-f]{4})】")
LABEL = re.compile(r"[\[【]编号[ \t]+m(?:[0-9a-f]{4}|\.{3}|…{1,2})[\]】] ?")
MESSAGE_ID = re.compile(r"-?[1-9][0-9]{0,19}")
RULES = (
    "\n主人消息和你先前回复开头的[编号 m加四位字符]是本地编号，不是原话的一部分。"
    "想明确回应较早的某一句（主人说的或你自己说过的）时，可以在回复最前面写【引用 编号】，"
    "例如【引用 m1a2b】；回应的就是主人最新一条时不用写。不要编造编号，回复里不要写"
    "[编号 …]，也不要在其他地方使用【引用】格式。"
)


def alias(record_id):
    return "m" + hashlib.sha256(record_id.encode()).hexdigest()[:4]


def label(message, record_id):
    """Prefix one rendered message with its alias without changing its source binding."""
    tag = "[编号 " + alias(record_id) + "] "
    content = message["content"]
    if isinstance(content, str):
        return {**message, "content": tag + content}
    parts = list(content)
    for index, part in enumerate(parts):
        if part.get("type") == "text":
            parts[index] = {**part, "text": tag + part["text"]}
            break
    return {**message, "content": parts}


def first_reply_part(source):
    """The first delivered bubble of an answered turn; that is the one QQ shows quoted."""
    if not source.get("reply_text"):
        return None
    parts = source.get("reply_segments")
    return parts[0]["message_id"] if parts else source.get("message_id")


def label_turn(turn, source):
    """Label the owner's text and the assistant reply in a rendered history turn."""
    reply_id, labelled, owner_done, reply_done = first_reply_part(source), [], False, False
    for message in turn:
        if (not owner_done and message["role"] == "user"
                and message["content"] == source["user_text"]):
            message, owner_done = label(message, source["event_id"]), True
        elif (not reply_done and reply_id and message["role"] == "assistant"
              and message["content"] == source["reply_text"]):
            message, reply_done = label(message, reply_id), True
        labelled.append(message)
    return labelled


def extract(reply):
    """Return the first chosen alias and the reply text without quote markers or labels."""
    found = CHOICE.search(reply)
    return (found.group(1) if found else None), LABEL.sub("", CHOICE.sub("", reply)).strip()


def _owner_id(store, job, row):
    if (row["conversation_id"] != job["conversation_id"] or row["actor_id"] != "owner"
            or row["source"] != "qq.onebot" or row["privacy_scope"] != "private"
            or not reply_revision.source_active(store, row["event_id"])
            or input_recall.is_recalled(store, row["source_event_id"])):
        return None
    return row["source_event_id"].rpartition(":")[2]


def _reply_id(store, job, message_id):
    from companion.transport import digest

    row = store.one('SELECT outbox.message_id AS message_id, outbox.origin_event_id AS '
        'origin_event_id, outbox.conversation_id AS conversation_id, outbox.action_type AS '
        'action_type, outbox.content_json AS content_json, outbox.content_sha256 AS '
        'content_sha256, outbox.approval_id AS approval_id, outbox.state AS state, '
        'outbox.delivery_key AS delivery_key, outbox.lease_owner AS lease_owner, '
        'outbox.lease_until AS lease_until, outbox.attempt_count AS attempt_count, '
        'outbox.provider_message_id AS provider_message_id, outbox.last_error_code AS '
        'last_error_code, outbox.created_at AS created_at, outbox.updated_at AS updated_at, '
        'outbox.sent_at AS sent_at, outbox.lease_token AS lease_token, outbox.next_attempt_at AS '
        'next_attempt_at, outbox.reply_group_id AS reply_group_id, outbox.segment_index AS '
        'segment_index, outbox.segment_count AS segment_count, outbox.reply_group_sha256 AS '
        'reply_group_sha256, outbox.segment_interval_ms AS segment_interval_ms, '
        'outbox.delivery_lane AS delivery_lane, outbox.native_ack_json AS native_ack_json, '
        'outbox.authority_json AS authority_json FROM outbox AS outbox WHERE outbox.message_id = '
        '%s', (message_id,))
    if (not row or row["state"] != "SENT" or row["action_type"] != "qq.private.reply"
            or row["conversation_id"] != job["conversation_id"]
            or not row["provider_message_id"] or not row["content_sha256"]
            or not reply_revision.source_active(store, row["origin_event_id"])):
        return None
    content = json.loads(row["content_json"])
    # Erasure clears content and its digest; never quote a reply that no longer exists.
    if not content.get("text") or digest(content) != row["content_sha256"]:
        return None
    return row["provider_message_id"]


def target(store, settings, clock, job, chosen):
    """Resolve the provider message id to quote, or None.

    never: no quote. occasionally: quote an older message the model named (owner or
    assistant), or the current input when it answers several coalesced inputs. often:
    also quote the current input when named. The id always comes from Core's records.
    """
    from companion import owner_input

    if settings.quote_policy == "never" or job["kind"] != "chat":
        return None
    current = store.one('SELECT events.event_id AS event_id, events.schema_version AS '
        'schema_version, events.source AS source, events.source_event_id AS source_event_id, '
        'events.conversation_id AS conversation_id, events.actor_id AS actor_id, events.event_type '
        'AS event_type, events.occurred_at AS occurred_at, events.ingested_at AS ingested_at, '
        'events.privacy_scope AS privacy_scope, events.body_json AS body_json, events.trace_id AS '
        'trace_id, events.idempotency_key AS idempotency_key, events.fingerprint AS fingerprint, '
        'events.redacted AS redacted, events.memory_processed AS memory_processed, '
        'events.reply_revision AS reply_revision FROM events AS events WHERE events.event_id = '
        '%s', (job["event_id"],))
    if not current:
        return None
    sources = owner_input.history_sources(store, settings, clock, job)
    # Unanswered inputs right before this one were folded into this reply.
    coalesced = bool(sources) and sources[-1].get("reply_complete") is False and not (
        sources[-1]["reply_text"])
    candidates = [("owner", row["event_id"], row) for row in (*sources, current)]
    candidates += [("reply", reply_id, source) for source in sources
                   if (reply_id := first_reply_part(source))]
    if chosen is None:
        if not coalesced:
            return None
        kind, row = "owner", current
    else:
        matches = {(kind, key): row for kind, key, row in candidates if alias(key) == chosen}
        if len(matches) != 1:
            return None
        (kind, key), row = next(iter(matches.items()))
        if (kind == "owner" and row["event_id"] == current["event_id"] and not coalesced
                and settings.quote_policy != "often"):
            return None
    message_id = _owner_id(store, job, row) if kind == "owner" else _reply_id(store, job, key)
    return message_id if message_id and MESSAGE_ID.fullmatch(message_id) else None
