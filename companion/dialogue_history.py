"""Recent delivered conversation turns and their local soft context bound.

Configured callers share full-request measurement with paid admission. Legacy callers
without settings use the historical byte estimate. Neither grants source authority.
"""

import json

from companion import qq_quotes, reply_revision
from companion.budget import token_upper_bound
from companion.contracts import CoreError
from companion.qq_rich import context


def recent_sent_turns(store, conversation_id, current_event_id, max_turns):
    """Return oldest first, with one complete private SENT reply per owner input."""
    return [turn_messages(row) for row in recent_sent_sources(
        store, conversation_id, current_event_id, max_turns,
    )]


def turn_messages(source, store=None, query=""):
    """With a store, quotes of since erased or recalled messages are withdrawn and read
    attachments are replayed, focused on `query` when one is given."""
    rich = observed = None
    if source.get("body_json"):
        body = json.loads(source["body_json"])
        rich = context(body, qq_quotes.withdrawn(store, source["event_id"], body)
                       if store is not None else frozenset())
    if store is not None and source.get("event_id"):
        from companion.memory_references import HISTORY_HEADING, replay
        from companion.qq_rich import encoded

        if notes := replay(store, source["event_id"], query):
            observed = {"role": "user", "content": HISTORY_HEADING + encoded(notes)}
    return [*([rich] if rich else []), *([observed] if observed else []),
            {"role": "user", "content": source["user_text"]},
            *([{"role": "assistant", "content": source["reply_text"]}]
              if source["reply_text"] else [])]


def recent_sent_sources(store, conversation_id, current_event_id, max_turns, *, event_ids=None,
                        include_partial=False, cutoff_rowid=None):
    """Return source-bound turns; an optional small allowlist is applied before LIMIT."""
    from companion.transport import digest, reply_group, segment_envelopes

    if max_turns <= 0:
        return []
    if event_ids is not None and not event_ids:
        return []
    allowed = ""
    if event_ids is not None:
        allowed = "AND e.event_id IN (" + ",".join("?" for _ in event_ids) + ") "
    cutoff = ("e.rowid<=?" if cutoff_rowid is not None
              else "e.rowid<(SELECT rowid FROM events WHERE event_id=?)")
    completeness = (
        "AND ((reply.reply_group_id IS NULL AND reply.segment_index=0 "
        "AND reply.segment_count=1 AND reply.reply_group_sha256 IS NULL "
        "AND reply.segment_interval_ms=0 AND reply.delivery_key NOT LIKE 'reply:%:v2:%') "
        "OR (reply.reply_group_id IS NOT NULL AND "
        "(SELECT COUNT(*) FROM outbox part WHERE part.reply_group_id=reply.reply_group_id) "
        "=reply.segment_count AND NOT EXISTS (SELECT 1 FROM outbox part "
        "WHERE part.reply_group_id=reply.reply_group_id AND part.state!='SENT'))) "
    ) if not include_partial else ""
    rows = store.rows(
        "SELECT e.*,o.message_id,o.content_json,o.content_sha256,o.delivery_key,o.reply_group_id,"
        "json_extract(e.body_json,'$.text') AS user_text,"
        "json_extract(o.content_json,'$.text') AS reply_text FROM events e "
        "JOIN outbox o ON o.origin_event_id=e.event_id AND o.rowid IN "
        "(SELECT MAX(reply.rowid) FROM outbox reply WHERE reply.conversation_id=? "
        "AND reply.action_type='qq.private.reply' AND reply.state='SENT' "
        "AND json_type(reply.content_json,'$.text')='text' "
        + completeness +
        "AND length(json_extract(reply.content_json,'$.text'))>0 GROUP BY reply.origin_event_id) "
        "WHERE e.conversation_id=? AND e.actor_id='owner' AND e.privacy_scope='private' "
        "AND e.source='qq.onebot' "
        "AND e.event_type IN ('message.received','message.media_received') "
        "AND e.redacted=0 AND " + cutoff + " "
        "AND json_type(e.body_json,'$.text')='text' "
        "AND (length(json_extract(e.body_json,'$.text'))>0 "
        "OR json_type(e.body_json,'$.rich')='object') "
        "AND COALESCE(json_extract(e.body_json,'$.diary_command'),0)=0 "
        "AND COALESCE(json_extract(e.body_json,'$.control_command'),0)=0 "
        "AND NOT EXISTS (SELECT 1 FROM memory_evidence me "
        "JOIN memories m ON m.memory_id=me.memory_id WHERE me.event_id=e.event_id "
        "AND m.status IN ('SUPERSEDED','FORGOTTEN')) " + allowed + "ORDER BY e.rowid DESC LIMIT ?",
        (conversation_id, conversation_id,
         cutoff_rowid if cutoff_rowid is not None else current_event_id,
         *(event_ids or ()), max_turns),
    )
    sources = []
    for row in rows:
        # This operational column is outside historical input and context fingerprints.
        row.pop("reply_revision", None)
        if not reply_revision.source_active(store, row["event_id"]):
            continue
        if row.pop("reply_group_id") is not None:
            try:
                group = reply_group(store, row["message_id"], require_sent=not include_partial)
                if not group:
                    continue
                parts = group["rows"]
                if include_partial:
                    prefix = []
                    for part in parts:
                        if part["state"] != "SENT":
                            break
                        if not part["sent_at"]:
                            raise CoreError("REPLY_GROUP_CHANGED", 409)
                        prefix.append(part)
                    if (not prefix or any(part["state"] == "SENT" for part in parts[len(prefix):])
                        or prefix[-1]["message_id"] != row["message_id"]):
                        continue
                    if len(prefix) < len(parts):
                        row["reply_complete"] = False
                    parts = prefix
                row["reply_text"] = "".join(json.loads(part["content_json"])["text"]
                                           for part in parts)
                envelopes = segment_envelopes(group)
                row["reply_segments"] = [
                    {"message_id": part["message_id"],
                     **envelopes[part["message_id"]]}
                    for part in parts
                ]
            except CoreError:
                continue
        elif include_partial:
            delivered = store.one("SELECT * FROM outbox WHERE message_id=?", (row["message_id"],))
            content = json.loads(row["content_json"])
            if (not delivered or not delivered["sent_at"] or delivered["segment_index"] != 0
                or delivered["segment_count"] != 1 or delivered["reply_group_sha256"] is not None
                or delivered["segment_interval_ms"] != 0 or ":v2:" in row["delivery_key"]
                or not isinstance(content, dict) or not isinstance(content.get("text"), str)
                or digest(content) != row["content_sha256"]):
                continue
        sources.append(row)
    return list(reversed(sources))


def pending_owner_sources(store, conversation_id, current_event_id, max_turns, *,
                          cutoff_rowid=None):
    """Keep unanswered admitted inputs in order: superseded replies and failed jobs."""
    if max_turns <= 0:
        return []
    cutoff = ("e.rowid<=?" if cutoff_rowid is not None
              else "e.rowid<(SELECT rowid FROM events WHERE event_id=?)")
    rows = store.rows(
        "SELECT e.*,json_extract(e.body_json,'$.text') AS user_text,'' AS reply_text "
        "FROM events e JOIN jobs j USING(event_id) WHERE e.conversation_id=? "
        "AND e.actor_id='owner' AND e.source='qq.onebot' AND e.privacy_scope='private' "
        "AND e.event_type IN ('message.received','message.media_received') "
        "AND e.redacted=0 AND e.reply_revision>0 AND j.kind='chat' "
        "AND ((j.state='CANCELLED' AND j.last_error_code=?) OR j.state='FAILED') "
        "AND " + cutoff + " "
        "AND COALESCE(json_extract(e.body_json,'$.diary_command'),0)=0 "
        "AND COALESCE(json_extract(e.body_json,'$.control_command'),0)=0 "
        "AND (length(json_extract(e.body_json,'$.text'))>0 "
        "OR json_type(e.body_json,'$.rich')='object') "
        "AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.origin_event_id=e.event_id "
        "AND o.action_type='qq.private.reply' AND o.state='SENT') "
        "ORDER BY e.rowid DESC LIMIT ?",
        (conversation_id, reply_revision.SUPERSEDED,
         cutoff_rowid if cutoff_rowid is not None else current_event_id, max_turns),
    )
    for row in rows:
        row.pop("reply_revision", None)
        row["reply_complete"] = False
    return [row for row in reversed(rows) if reply_revision.source_active(store, row["event_id"])]


def recent_context_sources(store, conversation_id, current_event_id, max_turns, *,
                           cutoff_rowid=None):
    """Combine observed sent prefixes with unanswered inputs; never invent assistant text."""
    sources = recent_sent_sources(store, conversation_id, current_event_id, max_turns,
                                  include_partial=True, cutoff_rowid=cutoff_rowid)
    sources += pending_owner_sources(store, conversation_id, current_event_id, max_turns,
                                     cutoff_rowid=cutoff_rowid)
    if not sources:
        return []
    marks = ",".join("?" for _ in sources)
    order = {row["event_id"]: row["rowid"] for row in store.rows(
        f"SELECT event_id,rowid FROM events WHERE event_id IN ({marks})",
        tuple(source["event_id"] for source in sources),
    )}
    return sorted(sources, key=lambda row: order[row["event_id"]])[-max_turns:]


def fit_context(rules, references, turns, current_input, soft_limit, *, settings=None,
                scenario="chat", blocks=None, dropped=None):
    """Apply the same trusted priority order as final request planning.

    Current input also includes its bounded shared-content reference, when present.
    If those required parts exceed the soft bound, leave them complete for admission.
    """
    from companion.context_selection import BACKGROUND, OLD_HISTORY, TOPIC, drop_order

    if blocks is None:
        blocks = [("rules", [rules], True, TOPIC),
                  *(("reference", [message], False, BACKGROUND) for message in references),
                  *(("history", turn, False, OLD_HISTORY) for turn in turns),
                  ("current", current_input, True, TOPIC)]
    groups, offset = [], 0
    for kind, block, required, priority in blocks:
        groups.append({"wire_start": offset, "priority": priority,
                       "index": len(groups),
                       "required": required or any(isinstance(m["content"], list) for m in block),
                       "block": block, "kind": kind})
        offset += len(block)
    messages = [message for group in groups for message in group["block"]]
    def reason():
        if settings is None:
            return "SOFT_LIMIT" if token_upper_bound(messages) > soft_limit else None
        from companion.context_planner import pressure
        from companion.token_measurement import measure_request

        measured = measure_request(settings, messages, scenario)
        return pressure(settings, measured, soft_limit=soft_limit, scenario=scenario)

    for group in drop_order(groups):
        cause = reason()
        if cause is None:
            break
        if dropped is not None:
            dropped.append({"index": group["index"], "kind": group["kind"],
                            "priority": group["priority"], "reason": cause})
        removed = {id(message) for message in group["block"]}
        messages = [message for message in messages if id(message) not in removed]
    return messages
