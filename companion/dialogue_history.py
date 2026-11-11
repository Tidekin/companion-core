"""Recent delivered conversation turns and their local soft context bound.

Configured callers share full-request measurement with paid admission. Legacy callers
without settings use the historical byte estimate. Neither grants source authority.
"""

import json
from dataclasses import dataclass, field

from companion import qq_quotes, reply_revision
from companion.budget import token_upper_bound
from companion.contracts import CoreError
from companion.qq_rich import context


@dataclass
class ReplyHistory:
    sources: list
    turns: list
    continuation: dict | None
    review_target: dict | None
    review_proofs: list
    recalled: dict | None = None
    recalled_reference: dict | None = None
    recalled_history: list = field(default_factory=list)
    original_order: dict = field(default_factory=dict)

    def retained(self, messages):
        visible = {id(message) for message in messages}
        return [source for source, turn in zip(self.sources, self.turns, strict=True)
                if id(turn[0]) in visible]

    def include_review_sources(self, retained, messages):
        """A retained excerpt may depend on a trimmed turn's original input and delivered reply."""
        visible = {id(message) for message in messages}
        reviews = [(used, proof) for turn, used, proof in self.review_proofs
                   if id(turn[0]) in visible]
        review_ids = {source_id for used, _ in reviews for source_id in used}
        if self.review_target and not self.review_target["ambiguous"]:
            review_ids.add(self.review_target["event_id"])
        extra = review_ids - {source["event_id"] for source in retained}
        retained.extend(source for source in self.sources if source["event_id"] in extra)
        return review_ids, [{"kind": "memory", "proof": proof} for _, proof in reviews]


def reply_sources(store, settings, clock, job, *, authority, bounded_text, followup):
    from companion import live_text, owner_input, private_life

    if authority:
        sources = owner_input.history_sources(store, settings, clock, job,
            purpose="proactive" if followup else "chat") if (
            "history" in authority.policy.purposes and (job["kind"] == "chat" or followup)
        ) else []
    elif bounded_text:
        sources = live_text.history_sources(store, settings, clock, job)
    else:
        sources = recent_sent_sources(
            store, job["conversation_id"], job["event_id"], settings.max_recent_messages // 2)
        if private_life.enabled(settings):
            sources = private_life.recent_history(store, settings, job)
    return sources


def recent_sent_turns(store, conversation_id, current_event_id, max_turns):
    """Return oldest first, with one complete private SENT reply per owner input."""
    return [
        turn_messages(row)
        for row in recent_sent_sources(
            store,
            conversation_id,
            current_event_id,
            max_turns,
        )
    ]


def turn_messages(source, store=None, query=""):
    """With a store, quotes of since erased or recalled messages are withdrawn and read
    attachments are replayed, focused on `query` when one is given."""
    rich = observed = None
    if source.get("body_json"):
        body = json.loads(source["body_json"])
        rich = context(
            body,
            qq_quotes.withdrawn(store, source["event_id"], body)
            if store is not None
            else frozenset(),
        )
    if store is not None and source.get("event_id"):
        from companion.memory_references import HISTORY_HEADING, replay
        from companion.qq_rich import encoded

        if notes := replay(store, source["event_id"], query):
            observed = {"role": "user", "content": HISTORY_HEADING + encoded(notes)}
    return [
        *([rich] if rich else []),
        *([observed] if observed else []),
        {"role": "user", "content": source["user_text"]},
        *([{"role": "assistant", "content": source["reply_text"]}] if source["reply_text"] else []),
    ]


def recent_sent_sources(
    store,
    conversation_id,
    current_event_id,
    max_turns,
    *,
    event_ids=None,
    include_partial=False,
    cutoff_rowid=None,
    _nominate=None,
):
    """Return source-bound turns; an optional small allowlist is applied before LIMIT."""
    from companion.transport import digest, reply_group, segment_envelopes

    if max_turns <= 0:
        return []
    if event_ids is not None and not event_ids:
        return []
    allowed = ""
    if event_ids is not None:
        allowed = "AND e.event_id IN (" + ",".join('%s' for _ in event_ids) + ") "
    cutoff = (
        "e.record_sequence<=%s"
        if cutoff_rowid is not None
        else 'e.record_sequence<(SELECT record_sequence FROM events WHERE event_id=%s)'
    )
    completeness = (
        (
            (
                'AND ((reply.reply_group_id IS NULL AND '
                'reply.segment_index=0 AND reply.segment_count=1 AND '
                'reply.reply_group_sha256 IS NULL AND '
                'reply.segment_interval_ms=0 AND reply.delivery_key NOT '
                "LIKE 'reply:%%:v2:%%') OR (reply.reply_group_id IS NOT "
                'NULL AND (SELECT COUNT(*) FROM outbox part WHERE '
                'part.reply_group_id=reply.reply_group_id) '
                '=reply.segment_count AND NOT EXISTS (SELECT 1 FROM '
                'outbox part WHERE part.reply_group_id=reply.reply_group'
                "_id AND part.state!='SENT')) OR EXISTS (SELECT 1 FROM "
                'runtime_meta rr JOIN jobs rj ON rj.job_id=(CASE WHEN '
                "rr.value IS JSON THEN (rr.value::json#>>'{job_id}') "
                "END) WHERE rr.key='owner_reply_recovery_original:'||rep"
                "ly.reply_group_id AND rj.kind='reply_recovery' AND "
                "rj.state='SUCCEEDED' AND EXISTS (SELECT 1 FROM outbox "
                'rp WHERE rp.origin_event_id=rj.event_id) AND NOT '
                'EXISTS (SELECT 1 FROM outbox rp WHERE '
                "rp.origin_event_id=rj.event_id AND rp.state!='SENT'))) "
            )
        )
        if not include_partial
        else ""
    )
    frozen = store.frozen_scope if include_partial and _nominate is None else None
    delivery_projection = (
        ",jsonb_build_object('sent_at',o.sent_at,'segment_index',o.segment_index,"
        "'segment_count',o.segment_count,'reply_group_sha256',o.reply_group_sha256,"
        "'segment_interval_ms',o.segment_interval_ms) AS history_delivery "
        if frozen is not None else ""
    )
    projection = (
        "SELECT e.event_id,octet_length(e.body_json)+octet_length(o.content_json) AS text_bytes "
        if _nominate is not None
        else (

                'SELECT e.*,o.message_id,o.content_json,'
                'o.content_sha256,o.delivery_key,o.reply_group_id,(CASE '
                'WHEN e.body_json IS JSON THEN '
                "(e.body_json::json#>>'{text}') END) AS user_text,(CASE "
                'WHEN o.content_json IS JSON THEN '
                "(o.content_json::json#>>'{text}') END) AS reply_text "

        )
    )
    read = (
        store.rows
        if _nominate is None
        else lambda sql, args: _nominate.rows(store, sql, args, limit=max_turns)
    )
    rows = read(
        projection + delivery_projection
        + (

                'FROM events e JOIN outbox o ON '
                'o.record_sequence=(SELECT MAX(reply.record_sequence) '
                'FROM outbox reply WHERE '
                'reply.origin_event_id=e.event_id AND '
                'reply.conversation_id=%s AND '
                "reply.action_type IN ('qq.private.reply','native.private.reply') AND "
                "reply.state='SENT' AND "
                "json_typeof(reply.content_json::json#>'{text}')='string"
                "' "

        )
        + completeness
        + (

                'AND length((CASE WHEN reply.content_json IS JSON THEN '
                "(reply.content_json::json#>>'{text}') END))>0) WHERE "
                "e.conversation_id=%s AND e.actor_id='owner' AND "
                "e.privacy_scope='private' AND e.source IN ('qq.onebot','native.chat') AND "
                "e.event_type IN ('message.received',"
                "'message.media_received') AND e.redacted=0 AND "

        )
        + cutoff
        + (

                " AND json_typeof(e.body_json::json#>'{text}')='string' "
                'AND (length((CASE WHEN e.body_json IS JSON THEN '
                "(e.body_json::json#>>'{text}') END))>0 OR "
                "json_typeof(e.body_json::json#>'{rich}')='object') AND "
                'COALESCE((CASE WHEN e.body_json IS JSON THEN CASE '
                "(e.body_json::json#>>'{diary_command}') WHEN 'true' "
                "THEN 1 WHEN 'false' THEN 0 WHEN '1' THEN 1 WHEN '0' "
                'THEN 0 END END),0)=0 AND COALESCE((CASE WHEN '
                'e.body_json IS JSON THEN CASE '
                "(e.body_json::json#>>'{control_command}') WHEN 'true' "
                "THEN 1 WHEN 'false' THEN 0 WHEN '1' THEN 1 WHEN '0' "
                'THEN 0 END END),0)=0 AND NOT EXISTS (SELECT 1 FROM '
                'memory_evidence me JOIN memories m ON '
                'm.memory_id=me.memory_id WHERE me.event_id=e.event_id '
                "AND m.status IN ('SUPERSEDED','FORGOTTEN')) "

        )
        + allowed
        + 'ORDER BY e.record_sequence DESC LIMIT %s',
        (
            conversation_id,
            conversation_id,
            cutoff_rowid if cutoff_rowid is not None else current_event_id,
            *(event_ids or ()),
            max_turns,
        ),
    )
    if _nominate is not None:
        return rows  # Identity/size nominations only; callers must read and validate live sources.
    active = reply_revision.active_sources(store, [row["event_id"] for row in rows])
    sources = []
    for row in rows:
        delivery = row.pop("history_delivery", None)
        # This operational column is outside historical input and context fingerprints.
        row.pop("reply_revision", None)
        if row["event_id"] not in active:
            continue
        if row.pop("reply_group_id") is not None:
            try:
                from companion.reply_recovery import history_group

                group = history_group(store, row["message_id"], include_partial=include_partial)
                group = group or reply_group(
                    store, row["message_id"], require_sent=not include_partial
                )
                if not group:
                    continue
                parts = group["rows"]
                if group.get("envelopes"):
                    delivered = [part for part in parts if part["state"] == "SENT"]
                    if delivered:
                        # The physical SQL row belongs to the original ACK prefix;
                        # the logical history now ends at the copied suffix's ACK.
                        for key in ("message_id", "content_json", "content_sha256", "delivery_key"):
                            row[key] = delivered[-1][key]
                if include_partial:
                    prefix = []
                    for part in parts:
                        if part["state"] != "SENT":
                            break
                        if not part["sent_at"]:
                            raise CoreError("REPLY_GROUP_CHANGED", 409)
                        prefix.append(part)
                    if (
                        not prefix
                        or any(part["state"] == "SENT" for part in parts[len(prefix) :])
                        or prefix[-1]["message_id"] != row["message_id"]
                    ):
                        continue
                    if len(prefix) < len(parts):
                        row["reply_complete"] = False
                    parts = prefix
                row["reply_text"] = "".join(
                    json.loads(part["content_json"])["text"] for part in parts
                )
                envelopes = group.get("envelopes") or segment_envelopes(group)
                row["reply_segments"] = [
                    {"message_id": part["message_id"], **(envelopes[part["message_id"]] or {})}
                    for part in parts
                ]
            except CoreError:
                continue
        elif include_partial:
            # The same immutable snapshot already read these exact outbox fields.
            # Writers and unowned transactions still take their fresh per-row read.
            delivered = delivery if (
                frozen is not None and store.frozen_scope == frozen
            ) else store.one(
                "SELECT outbox.message_id AS message_id, outbox.origin_event_id "
                "AS origin_event_id, outbox.conversation_id AS conversation_id, outbox.action_type "
                "AS action_type, outbox.content_json AS content_json, outbox.content_sha256 AS "
                "content_sha256, outbox.approval_id AS approval_id, outbox.state AS state, "
                "outbox.delivery_key AS delivery_key, outbox.lease_owner AS lease_owner, "
                "outbox.lease_until AS lease_until, outbox.attempt_count AS attempt_count, "
                "outbox.provider_message_id AS provider_message_id, outbox.last_error_code AS "
                "last_error_code, outbox.created_at AS created_at, outbox.updated_at AS "
                "updated_at, outbox.sent_at AS sent_at, outbox.lease_token AS lease_token, "
                "outbox.next_attempt_at AS next_attempt_at, outbox.reply_group_id AS "
                "reply_group_id, outbox.segment_index AS segment_index, outbox.segment_count AS "
                "segment_count, outbox.reply_group_sha256 AS reply_group_sha256, "
                "outbox.segment_interval_ms AS segment_interval_ms, outbox.delivery_lane AS "
                "delivery_lane, outbox.native_ack_json AS native_ack_json, outbox.authority_json "
                "AS authority_json FROM outbox AS outbox WHERE outbox.message_id = "
                "%s",
                (row["message_id"],),
            )
            content = json.loads(row["content_json"])
            if (
                not delivered
                or not delivered["sent_at"]
                or delivered["segment_index"] != 0
                or delivered["segment_count"] != 1
                or delivered["reply_group_sha256"] is not None
                or delivered["segment_interval_ms"] != 0
                or ":v2:" in row["delivery_key"]
                or not isinstance(content, dict)
                or not isinstance(content.get("text"), str)
                or digest(content) != row["content_sha256"]
            ):
                continue
        sources.append(row)
    return list(reversed(sources))


def pending_owner_sources(
    store, conversation_id, current_event_id, max_turns, *, cutoff_rowid=None
):
    """Keep unanswered admitted inputs in order: superseded replies and failed jobs."""
    if max_turns <= 0:
        return []
    cutoff = (
        "e.record_sequence<=%s"
        if cutoff_rowid is not None
        else 'e.record_sequence<(SELECT record_sequence FROM events WHERE event_id=%s)'
    )
    rows = store.rows(
        (
            (
                'SELECT e.*,(CASE WHEN e.body_json IS JSON THEN '
                "(e.body_json::json#>>'{text}') END) AS user_text,'' AS "
                'reply_text FROM events e JOIN jobs j USING(event_id) '
                "WHERE e.conversation_id=%s AND e.actor_id='owner' AND "
                "e.source IN ('qq.onebot','native.chat') AND e.privacy_scope='private' AND "
                "e.event_type IN ('message.received',"
                "'message.media_received') AND e.redacted=0 AND "
                "e.reply_revision>0 AND j.kind='chat' AND "
                "((j.state='CANCELLED' AND j.last_error_code=%s) OR "
                "j.state='FAILED') AND "
            )
        )
        + cutoff
        + (

                ' AND COALESCE((CASE WHEN e.body_json IS JSON THEN CASE '
                "(e.body_json::json#>>'{diary_command}') WHEN 'true' "
                "THEN 1 WHEN 'false' THEN 0 WHEN '1' THEN 1 WHEN '0' "
                'THEN 0 END END),0)=0 AND COALESCE((CASE WHEN '
                'e.body_json IS JSON THEN CASE '
                "(e.body_json::json#>>'{control_command}') WHEN 'true' "
                "THEN 1 WHEN 'false' THEN 0 WHEN '1' THEN 1 WHEN '0' "
                'THEN 0 END END),0)=0 AND (length((CASE WHEN '
                'e.body_json IS JSON THEN '
                "(e.body_json::json#>>'{text}') END))>0 OR "
                "json_typeof(e.body_json::json#>'{rich}')='object') AND "
                'NOT EXISTS (SELECT 1 FROM outbox o WHERE '
                'o.origin_event_id=e.event_id AND '
                "o.action_type IN ('qq.private.reply','native.private.reply') AND o.state='SENT') "
                'ORDER BY e.record_sequence DESC LIMIT %s'

        ),
        (
            conversation_id,
            reply_revision.SUPERSEDED,
            cutoff_rowid if cutoff_rowid is not None else current_event_id,
            max_turns,
        ),
    )
    for row in rows:
        row.pop("reply_revision", None)
        row["reply_complete"] = False
    active = reply_revision.active_sources(store, [row["event_id"] for row in rows])
    return [row for row in reversed(rows) if row["event_id"] in active]


def recent_context_sources(
    store, conversation_id, current_event_id, max_turns, *, cutoff_rowid=None
):
    """Combine observed sent prefixes with unanswered inputs; never invent assistant text."""
    sources = recent_sent_sources(
        store,
        conversation_id,
        current_event_id,
        max_turns,
        include_partial=True,
        cutoff_rowid=cutoff_rowid,
    )
    sources += pending_owner_sources(
        store, conversation_id, current_event_id, max_turns, cutoff_rowid=cutoff_rowid
    )
    if not sources:
        return []
    marks = ",".join('%s' for _ in sources)
    order = {
        row["event_id"]: row["rowid"]
        for row in store.rows(
            f'SELECT event_id,record_sequence AS rowid FROM events WHERE event_id IN ({marks})',
            tuple(source["event_id"] for source in sources),
        )
    }
    return sorted(sources, key=lambda row: order[row["event_id"]])[-max_turns:]


def fit_context(
    rules,
    references,
    turns,
    current_input,
    soft_limit,
    *,
    settings=None,
    scenario="chat",
    blocks=None,
    dropped=None,
):
    """Apply the same trusted priority order as final request planning.

    Current input also includes its bounded shared-content reference, when present.
    If those required parts exceed the soft bound, leave them complete for admission.
    """
    from companion.context_selection import BACKGROUND, OLD_HISTORY, TOPIC, drop_order

    if blocks is None:
        blocks = [
            ("rules", [rules], True, TOPIC),
            *(("reference", [message], False, BACKGROUND) for message in references),
            *(("history", turn, False, OLD_HISTORY) for turn in turns),
            ("current", current_input, True, TOPIC),
        ]
    groups, offset = [], 0
    for kind, block, required, priority in blocks:
        groups.append(
            {
                "wire_start": offset,
                "priority": priority,
                "index": len(groups),
                "required": required or any(isinstance(m["content"], list) for m in block),
                "block": block,
                "kind": kind,
            }
        )
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
            dropped.append(
                {
                    "index": group["index"],
                    "kind": group["kind"],
                    "priority": group["priority"],
                    "reason": cause,
                }
            )
        removed = {id(message) for message in group["block"]}
        messages = [message for message in messages if id(message) not in removed]
    return messages
