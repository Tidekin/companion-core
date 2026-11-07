"""Profile-purpose inputs from native owner decisions and complete Memory source proofs."""

import hashlib
import json
import re
from datetime import datetime

from companion.commands import command_parts
from companion.contracts import CoreError
from companion.memory import Memory
from companion.memory_context import MEMORY_FIELDS, MemoryContext, MemorySources
from companion.privacy import ErasureLedger, redact, sensitive
from companion.store import encode

ERROR = "SOCIAL_PROFILE_SOURCE_REQUIRED"
FIELDS = {"nickname", "signature"}


def _deny():
    raise CoreError(ERROR, 403)


def _console_receipt(memory, event):
    """A typed native Memory receipt, not a generic console.operation audit or status label."""
    body = json.loads(event["body_json"])
    expected = {
        "text",
        "channel",
        "origin",
        "operation_id",
        "request_sha256",
        "memory_id",
        "result_memory_id",
        "decision",
        "before_sha256",
        "after_sha256",
        "content_sha256",
        "source_event_ids",
    }
    if (
        not isinstance(body, dict)
        or set(body) != expected
        or event["source"] != "owner.decision"
        or event["event_type"] != "memory.console_decided"
        or event["actor_id"] != "owner"
        or event["privacy_scope"] != "private"
        or event["redacted"]
        or body["channel"] != "owner_console"
        or body["origin"] != memory.settings.console.public_origin
        or body["decision"] not in {"confirm", "correct"}
        or not isinstance(body["text"], str)
        or not isinstance(body["operation_id"], str)
        or event["source_event_id"] != "console-memory:" + body["operation_id"]
        or any(
            not isinstance(body[key], str) or not re.fullmatch(r"[a-f0-9]{64}", body[key])
            for key in ("request_sha256", "before_sha256", "after_sha256", "content_sha256")
        )
        or hashlib.sha256(body["text"].encode()).hexdigest() != body["content_sha256"]
    ):
        _deny()
    operation = memory.store.one(
        "SELECT console_operations.request_id AS request_id, "
        "console_operations.request_sha256 AS request_sha256, console_operations.state AS state, "
        "console_operations.result_json AS result_json, console_operations.created_at AS "
        "created_at, console_operations.updated_at AS updated_at FROM console_operations AS "
        "console_operations WHERE console_operations.request_id = %s",
        (body["operation_id"],),
    )
    request_hash = hashlib.sha256(
        encode(
            [
                "POST",
                "/v1/console/memories/" + body["memory_id"] + "/decision",
                {
                    "decision": body["decision"],
                    "corrected_content": body["text"] if body["decision"] == "correct" else None,
                },
            ]
        ).encode()
    ).hexdigest()
    if (
        not operation
        or operation["request_sha256"] != body["request_sha256"]
        or request_hash != body["request_sha256"]
        or operation["state"] not in {"STARTED", "COMPLETE"}
        or not body["source_event_ids"]
        or body["source_event_ids"]
        != [
            r["source_event_id"]
            for r in memory.store.rows(
                "SELECT source_event_id FROM event_dependencies WHERE event_id = %s "
                "ORDER BY source_event_id NULLS FIRST",
                (event["event_id"],),
            )
        ]
    ):
        _deny()
    audit = memory.store.one(
        (
            (
                'SELECT * FROM audit_log WHERE '
                "action='memory.owner_decision' AND entity_id=%s AND "
                'trace_id=%s AND (CASE WHEN safe_detail_json IS JSON '
                "THEN (safe_detail_json::json#>>'{source_event_id}') "
                'END)=%s AND (CASE WHEN safe_detail_json IS JSON THEN '
                "(safe_detail_json::json#>>'{channel}') "
                "END)='owner_console' ORDER BY record_sequence DESC "
                'LIMIT 1'
            )
        ),
        (body["memory_id"], event["trace_id"], event["event_id"]),
    )
    if (
        not audit
        or audit["created_at"] != event["ingested_at"]
        or json.loads(audit["safe_detail_json"])
        != {
            "decision": body["decision"],
            "source_event_id": event["event_id"],
            "channel": "owner_console",
            "receipt_sha256": hashlib.sha256(encode(body).encode()).hexdigest(),
        }
    ):
        _deny()
    return body, audit


def _owner_lineage(memory, event_id, now):
    sources = MemorySources(memory, now)
    if not sources.event(event_id):
        _deny()
    rows = list(sources._lineage(event_id))
    if not rows or len(rows) > 256:
        _deny()
    for row in rows:
        if (
            row["actor_id"] != "owner"
            or (row["source"], row["event_type"])
            not in {
                ("qq.onebot", "message.received"),
                ("owner.decision", "memory.corrected"),
                ("owner.decision", "memory.console_decided"),
            }
            or memory.store.one(
                "SELECT 1 FROM observations WHERE event_id = %s", (row["event_id"],)
            )
            or memory.store.one(
                "SELECT 1 FROM source_grant_events WHERE event_id = %s", (row["event_id"],)
            )
        ):
            _deny()
        if row["event_type"] == "memory.console_decided":
            _console_receipt(memory, row)
        conversation = memory.store.one(
            "SELECT conversations.conversation_id AS conversation_id, "
            "conversations.platform AS platform, conversations.platform_session_key AS "
            "platform_session_key, conversations.scope AS scope, conversations.owner_actor_id AS "
            "owner_actor_id, conversations.last_event_at AS last_event_at, "
            "conversations.created_at AS created_at, conversations.reply_revision AS "
            "reply_revision FROM conversations AS conversations WHERE "
            "conversations.conversation_id = %s",
            (row["conversation_id"],),
        )
        if (
            not conversation
            or conversation["platform"] != "qq.onebot"
            or conversation["platform_session_key"]
            != ("onebot-direct:FriendMessage:" + memory.settings.owner_qq_id)
        ):
            _deny()
    return rows


def _decision(memory, row, now):
    # Automatic CONFIRMED/user_confirmed labels are not an owner-decision receipt.
    for audit in memory.store.rows(
        "SELECT audit_log.audit_id AS audit_id, audit_log.action AS action, audit_log.actor_id AS "
        "actor_id, audit_log.entity_type AS entity_type, audit_log.entity_id AS entity_id, "
        "audit_log.trace_id AS trace_id, audit_log.safe_detail_json AS safe_detail_json, "
        "audit_log.created_at AS created_at FROM audit_log AS audit_log WHERE audit_log.action = "
        "'memory.owner_decision' AND audit_log.entity_id IN (%s, %s) ORDER BY audit_id DESC NULLS "
        "LAST LIMIT 64",
        (row["memory_id"], row["supersedes_memory_id"] or ""),
    ):
        detail = json.loads(audit["safe_detail_json"])
        event_id, decision = detail.get("source_event_id"), detail.get("decision")
        if not isinstance(event_id, str) or decision not in {"confirm", "correct"}:
            continue
        event = memory.store.one(
            "SELECT events.event_id AS event_id, events.schema_version AS "
            "schema_version, events.source AS source, events.source_event_id AS source_event_id, "
            "events.conversation_id AS conversation_id, events.actor_id AS actor_id, "
            "events.event_type AS event_type, events.occurred_at AS occurred_at, "
            "events.ingested_at AS ingested_at, events.privacy_scope AS privacy_scope, "
            "events.body_json AS body_json, events.trace_id AS trace_id, events.idempotency_key AS "
            "idempotency_key, events.fingerprint AS fingerprint, events.redacted AS redacted, "
            "events.memory_processed AS memory_processed, events.reply_revision AS reply_revision "
            "FROM events AS events WHERE events.event_id = %s",
            (event_id,),
        )
        if event and event["event_type"] == "memory.console_decided":
            body, verified = _console_receipt(memory, event)
            if (
                body["result_memory_id"] != row["memory_id"]
                or body["text"] != row["content"]
                or body["after_sha256"] != hashlib.sha256(encode(row).encode()).hexdigest()
                or body["memory_id"]
                != (
                    row["memory_id"]
                    if body["decision"] == "confirm"
                    else row["supersedes_memory_id"]
                )
            ):
                continue
            _owner_lineage(memory, event_id, now)
            return verified, event_id
        if (
            not event
            or event["source"] != "qq.onebot"
            or event["trace_id"] != audit["trace_id"]
            or datetime.fromisoformat(audit["created_at"]) > datetime.fromisoformat(now)
        ):
            continue
        command, args = command_parts(json.loads(event["body_json"]).get("text", ""))
        if decision == "confirm":
            valid = (
                audit["entity_id"] == row["memory_id"]
                and command == "确认这一条"
                and args == row["memory_id"]
            )
        else:
            valid = (
                audit["entity_id"] == row["supersedes_memory_id"]
                and command == "纠正这一条"
                and args.split(maxsplit=1) == [row["supersedes_memory_id"], row["content"]]
                and memory.store.one(
                    "SELECT 1 FROM memory_evidence AS v JOIN events AS e USING (event_id) "
                    "WHERE v.memory_id = %s AND v.polarity = 1 AND e.source = "
                    "'owner.decision' AND e.event_type = 'memory.corrected' AND e.body_json = "
                    "%s",
                    (row["memory_id"], encode({"text": row["content"]})),
                )
            )
        if valid:
            _owner_lineage(memory, event_id, now)
            return audit, event_id
    _deny()


def material(store, settings, now, memory_ids):
    """Reuse Memory pinning and erasure ancestry; no new authority or source ledger."""
    if not memory_ids or len(memory_ids) > settings.social_autonomy.max_source_refs:
        _deny()
    if len(set(memory_ids)) != len(memory_ids):
        _deny()
    memory = Memory(store, settings, ErasureLedger(settings.erasure_ledger))
    context, sources = MemoryContext(memory), MemorySources(memory, now)
    memory.require_owner("owner")
    result = []
    for memory_id in sorted(memory_ids):
        row = store.one(
            "SELECT memories.memory_id AS memory_id, memories.subject AS subject, "
            "memories.kind AS kind, memories.content AS content, memories.status AS status, "
            "memories.origin AS origin, memories.confidence AS confidence, memories.importance AS "
            "importance, memories.privacy_scope AS privacy_scope, memories.supersedes_memory_id AS "
            "supersedes_memory_id, memories.created_at AS created_at, memories.updated_at AS "
            "updated_at, memories.valid_from AS valid_from, memories.valid_to AS valid_to, "
            "memories.last_reviewed_at AS last_reviewed_at, memories.fact_key AS fact_key, "
            "memories.needs_review AS needs_review FROM memories AS memories WHERE "
            "memories.memory_id = %s",
            (memory_id,),
        )
        if (
            not row
            or row["status"] != "CONFIRMED"
            or row["needs_review"]
            or row["origin"] != "user_confirmed"
            or row["privacy_scope"] != "private"
            or row["confidence"] <= 0
            or not sources.current(row)
            or not (
                (row["subject"] == "self" and row["kind"] in {"fact", "preference"})
                or (row["subject"] == "user" and row["kind"] == "preference")
            )
            or sensitive(row["content"])
            or redact(row["content"], settings) != row["content"]
        ):
            _deny()
        evidence = store.rows(
            "SELECT memory_evidence.memory_id AS memory_id, "
            "memory_evidence.event_id AS event_id, memory_evidence.polarity AS polarity, "
            "memory_evidence.created_at AS created_at FROM memory_evidence AS memory_evidence "
            "WHERE memory_evidence.memory_id = %s ORDER BY memory_evidence.event_id NULLS FIRST",
            (memory_id,),
        )
        if not evidence or any(e["polarity"] != 1 for e in evidence):
            _deny()
        for event in evidence:
            _owner_lineage(memory, event["event_id"], now)
        audit, decision_event = _decision(memory, row, now)
        expected = {
            **{key: row[key] for key in MEMORY_FIELDS},
            "evidence_ids": [event["event_id"] for event in evidence],
        }
        proof = context.pin(now, rows=[expected], event_ids=[decision_event])
        result.append(
            {
                "memory_id": memory_id,
                "subject": row["subject"],
                "kind": row["kind"],
                "content": row["content"],
                "owner_decision": audit,
                "proof": proof,
            }
        )
    return result


def candidates(store, settings, now):
    if not settings.memory_enabled:
        return []
    ids = []
    for row in store.rows(
        "SELECT memory_id FROM memories WHERE status = 'CONFIRMED' AND needs_review = 0 AND "
        "((subject = 'self' AND kind IN ('fact', 'preference')) OR (subject = 'user' AND kind = "
        "'preference')) ORDER BY updated_at DESC NULLS LAST, memory_id NULLS FIRST LIMIT 40"
    ):
        try:
            material(store, settings, now, [row["memory_id"]])
        except CoreError:
            continue
        ids.append(row["memory_id"])
        if len(ids) >= settings.social_autonomy.max_source_refs:
            break
    return ids
