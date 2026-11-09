"""Read-only v1 evidence codec; original signatures never authorize new execution."""

import hashlib
import json
import re
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from companion import input_recall, owner_authority, paid_budget
from companion.config import LiveTextWindowApproval
from companion.contracts import CoreError
from companion.privacy import epoch
from companion.qq_rich import input_digest
from companion.store import encode


def capture_source(store, settings, approval, event_id, *, allow_commands=False,
                   historical=False):
    event = store.one(
        'SELECT e.event_id AS event_id, e.schema_version AS schema_version, e.source AS source, '
        'e.source_event_id AS source_event_id, e.conversation_id AS conversation_id, e.actor_id AS '
        'actor_id, e.event_type AS event_type, e.occurred_at AS occurred_at, e.ingested_at AS '
        'ingested_at, e.privacy_scope AS privacy_scope, e.body_json AS body_json, e.trace_id AS '
        'trace_id, e.idempotency_key AS idempotency_key, e.fingerprint AS fingerprint, e.redacted '
        'AS redacted, e.memory_processed AS memory_processed, e.reply_revision AS reply_revision, '
        'c.scope AS scope, c.owner_actor_id AS owner_actor_id FROM events AS e JOIN conversations '
        'AS c ON e.conversation_id = c.conversation_id WHERE e.event_id = %s', (event_id,),
    )
    receipt = store.one(
        'SELECT value FROM runtime_meta WHERE key = %s', ("live_text_input:" + event_id,),
    )
    try:
        value = json.loads(receipt["value"]) if receipt else {}
        body = json.loads(event["body_json"]) if event else {}
        capture = value.get("capture")
        if (
            not event or event["redacted"] or not isinstance(body, dict)
            or (event["source"], event["actor_id"], event["scope"], event["owner_actor_id"])
            != ("qq.onebot", "owner", "private", "owner")
            or event["event_type"] != "message.received"
            or ((body.get("control_command") or body.get("diary_command")) and not allow_commands)
            or body.get("media")
            or not isinstance(body.get("text"), str)
            or (body["text"].strip().startswith("/") and not allow_commands)
            or value.get("event_id") != event_id
            or value.get("window_sha256") != approval.plan_sha256
            or (value.get("privacy_epoch") != epoch(store) and not (
                type(value.get("privacy_epoch")) is int
                and 0 <= value["privacy_epoch"] < epoch(store)
                and (historical or approval.companion_scope == "owner_private_v1" or (
                    allow_commands and (body.get("control_command") or body.get("diary_command"))
                ))))
            or value.get("input_sha256") != input_digest(body)
            or not isinstance(capture, dict)
            or capture.get("source_event_id") != event["source_event_id"]
        ):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise CoreError("LIVE_SOURCE_CHANGED", 403) from None
    try:
        signed = {key: item for key, item in capture.items() if key != "signature"}
        for key in ("received_at", "occurred_at"):
            signed[key] = datetime.fromisoformat(capture[key]).isoformat()
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(approval.capture_public_key)).verify(
            bytes.fromhex(capture["signature"]),
            b"companion-core/onebot-capture/v1\x00" + encode(signed).encode(),
        )
        received, occurred = (
            datetime.fromisoformat(capture[key]) for key in ("received_at", "occurred_at")
        )
        prefix = (
            f"{settings.bot_qq_id}:onebot-direct:FriendMessage:{settings.owner_qq_id}:"
            f"epoch:{capture['protocol_epoch']}:"
        )
        if (
            capture["kind"] != "authenticated_onebot_ws"
            or capture["window_sha256"] != approval.plan_sha256
            or capture["event_id"] != event_id
            or capture["input_sha256"] != input_digest(body)
            or capture["identity_sha256"]
            != paid_budget.sha([settings.owner_qq_id, settings.bot_qq_id])
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", capture["protocol_epoch"])
            or not capture["source_event_id"].startswith(prefix)
            or not re.fullmatch(r"-?[1-9][0-9]{0,19}", capture["source_event_id"][len(prefix):])
            or event_id != "evt_" + hashlib.sha256(capture["source_event_id"].encode()).hexdigest()
            or occurred != datetime.fromisoformat(event["occurred_at"])
            or not approval.starts_at <= occurred <= received < approval.ends_at
            or not 0 <= (received - occurred).total_seconds() <= 30
        ):
            raise ValueError()
    except (ValueError, KeyError, TypeError, InvalidSignature):
        raise CoreError("LIVE_SOURCE_CAPTURE_CHANGED", 403) from None
    jobs = store.rows('SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, jobs.kind AS kind, '
        'jobs.state AS state, jobs.idempotency_key AS idempotency_key, jobs.attempts AS attempts, '
        'jobs.max_attempts AS max_attempts, jobs.lease_owner AS lease_owner, jobs.lease_until AS '
        'lease_until, jobs.next_attempt_at AS next_attempt_at, jobs.last_error_code AS '
        'last_error_code, jobs.created_at AS created_at, jobs.updated_at AS updated_at, '
        'jobs.lease_token AS lease_token, jobs.context_tracked AS context_tracked, '
        'jobs.authority_json AS authority_json FROM jobs AS jobs WHERE jobs.event_id = %s AND '
        "jobs.kind = 'chat'", (event_id,))
    if len(jobs) != 1:
        raise CoreError("LIVE_SOURCE_CHAT_JOB_REQUIRED", 403)
    return event, body, jobs[0]


def _meta(store, key):
    row = store.one('SELECT value FROM runtime_meta WHERE key = %s', (key,))
    return json.loads(row["value"]) if row else None


def approval(store, event_id):
    """Resolve the archived approval against its original independent journal."""
    try:
        receipt = _meta(store, "live_text_input:" + event_id)
        if not receipt:
            raise CoreError("LEGACY_OWNER_SIGNED_INPUT_REQUIRED", 403)
        archive = _meta(store, "live_text_window:" + receipt["window_sha256"])
        if not archive or archive.get("state") not in {"OPEN", "STOPPED", "RETIRED"}:
            raise CoreError("LEGACY_OWNER_ARCHIVE_REQUIRED", 403)
        policy = LiveTextWindowApproval.model_validate(archive["approval"])
        binding = archive["binding"]
        identity = store.one("SELECT value FROM runtime_meta WHERE key = 'model_budget_journal_id'")
        if (not policy.live_approved or policy.execution != "approved_live"
            or policy.plan_sha256 != receipt["window_sha256"]
            or binding["approval_sha256"] != paid_budget.sha(policy.model_dump(mode="json"))
            or binding["plan_sha256"] != policy.plan_sha256
            or binding["window_id"] != policy.window_id
            or binding["budget_journal_id"] != policy.budget_journal_id
            or not identity or identity["value"] != policy.budget_journal_id):
            raise ValueError("original approval binding changed")
        path = Path(binding["budget_journal_path"])
        paid_budget.journal(store, SimpleNamespace(journal_path=path),
                            allow_settlement=True, initialize=False)
        # journal() verifies the complete chain and native projections first. The
        # archived window need not be the current lifecycle or have a live lease.
        opened = any(row.get("kind") == "window_open"
                     and row.get("window_id") == policy.window_id
                     and row.get("plan_sha256") == policy.plan_sha256
                     for row in (json.loads(line) for line in path.read_text("utf-8").splitlines()))
        if not opened:
            raise ValueError("original window was never opened")
    except (ValueError, KeyError, TypeError, OSError):
        raise CoreError("LEGACY_OWNER_ARCHIVE_CHANGED", 403) from None
    return policy


def source(store, settings, view, event_id, now, *, purpose="history", allow_commands=False):
    """Validate original raw evidence under an explicit current history/memory scope."""
    if purpose not in {"history", "memory"}:
        raise CoreError("LEGACY_OWNER_READ_SCOPE_REQUIRED", 403)
    with nullcontext() if store.in_transaction else store.transaction():
        view = owner_authority.current(store, settings, lambda: now,
                                        purpose=purpose, authority=view.authority)
        policy = approval(store, event_id)
        event, body, job = capture_source(store, settings, policy, event_id,
                                          allow_commands=allow_commands, historical=True)
        receipt = _meta(store, "live_text_input:" + event_id)
        received = datetime.fromisoformat(receipt["capture"]["received_at"])
        if (event["privacy_scope"] != "private" or json.loads(job["authority_json"]) != {}
            or received > datetime.fromisoformat(now)
            or store.one('SELECT 1 FROM runtime_meta WHERE key = %s', ("erased_event:" + event_id,))
            or (datetime.fromisoformat(now) - received).total_seconds() >= 86400 * min(
                view.policy.source_retention_days, settings.raw_event_retention_days)
            or (purpose == "history" and (view.policy.history.scope != "owner_private"
                or datetime.fromisoformat(event["occurred_at"]) < view.policy.history.not_before))
            or (purpose == "memory" and view.policy.memory_scope != "owner_private")):
            raise CoreError("LEGACY_OWNER_SOURCE_SCOPE_CHANGED", 403)
        input_recall.require_not_recalled(store, event["source_event_id"])
        return event, body, job


def validate_turn(store, settings, view, item, now):
    """Only original, settled, acknowledged SENT prefixes contribute assistant text."""
    try:
        _validate_turn(store, settings, view, item, now)
    except (ValueError, KeyError, TypeError):
        raise CoreError("LEGACY_OWNER_SENT_PROOF_REQUIRED", 403) from None


def _validate_turn(store, settings, view, item, now):
    from companion.private_life import event_digest, settled_usage_unchanged
    from companion.transport import reply_group, segment_envelopes

    with nullcontext() if store.in_transaction else store.transaction():
        event, _, job = source(store, settings, view, item["event_id"], now)
        if not item.get("reply_text"):
            return
        policy = approval(store, item["event_id"])
        usage = store.rows('SELECT m.usage_id AS usage_id, m.request_id AS request_id, m.job_id AS '
            'job_id, m.scenario AS scenario, m.provider AS provider, m.model AS model, '
            'm.input_tokens AS input_tokens, m.cached_input_tokens AS cached_input_tokens, '
            'm.output_tokens AS output_tokens, m.billed_micro_usd AS billed_micro_usd, '
            'm.is_estimate AS is_estimate, m.occurred_at AS occurred_at, m.state AS state, '
            'm.pricing_known AS pricing_known, a.window_id AS window_id, a.journal_id AS '
            'journal_id, a.contract_sha256 AS contract_sha256, a.payload_sha256 AS payload_sha256, '
            'a.authority_json AS authority_json FROM model_usage AS m LEFT JOIN model_admissions '
            'AS a ON m.request_id = a.request_id WHERE m.job_id = %s ORDER BY m.record_sequence '
            'NULLS FIRST', (job["job_id"],))
        if (job["state"] != "SUCCEEDED" or not any(row["scenario"] == "chat" for row in usage)
            or any(row["state"] != "SETTLED" or row["window_id"] != policy.window_id
                or row["journal_id"] != policy.budget_journal_id
                or row["contract_sha256"] != policy.paid_contract_sha256
                or not row["authority_json"] or json.loads(row["authority_json"]) != {}
                for row in usage)):
            raise CoreError("LEGACY_OWNER_SETTLED_REPLY_REQUIRED", 403)
        if policy.companion_scope != "owner_private_v1":
            context = _meta(store, "live_text_context:" + job["job_id"])
            if (not context or context["plan_sha256"] != policy.plan_sha256
                or context["event_id"] != event["event_id"]
                or context["conversation_id"] != event["conversation_id"]
                or type(context["privacy_epoch"]) is not int
                or not 0 <= context["privacy_epoch"] <= epoch(store)
                or any(row["payload_sha256"] != context["payload_sha256"]
                       for row in usage if row["scenario"] == "chat")):
                raise CoreError("LEGACY_OWNER_SETTLED_REPLY_REQUIRED", 403)
        group = reply_group(store, item["message_id"])
        if not group:
            raise CoreError("LEGACY_OWNER_SENT_PROOF_REQUIRED", 403)
        envelopes = segment_envelopes(group)
        if (policy.max_reply_chars != "unlimited" and len(group["text"]) > policy.max_reply_chars
            or group["segmented"] and (policy.segmented_reply is None
                or policy.segmented_reply.max_segments != "unlimited"
                and len(group["rows"]) > policy.segmented_reply.max_segments
                or any(value["interval_ms"] != policy.segmented_reply.interval_ms
                       for value in envelopes.values()))):
            raise CoreError("LEGACY_OWNER_SENT_PROOF_REQUIRED", 403)
        parts = []
        for row in group["rows"]:
            if row["state"] != "SENT":
                break
            parts.append(row)
        if (not parts or parts[-1]["message_id"] != item["message_id"]
            or any(row["state"] == "SENT" for row in group["rows"][len(parts):])
            or "".join(json.loads(row["content_json"])["text"] for row in parts)
                != item["reply_text"]):
            raise CoreError("LEGACY_OWNER_SENT_PROOF_REQUIRED", 403)
        for row in parts:
            # The already verified journal validates every resolution projection
            # against this exact native outcome, including owner-confirmed SENT.
            acknowledged = row["provider_message_id"] or _meta(
                store, "delivery_resolution:" + row["message_id"])
            if (not row["sent_at"] or datetime.fromisoformat(row["sent_at"])
                    > datetime.fromisoformat(now) or not acknowledged
                or row["origin_event_id"] != event["event_id"]
                or row["conversation_id"] != event["conversation_id"]
                or row["action_type"] != "qq.private.reply"
                or json.loads(row["authority_json"]) != {}
                or row["content_sha256"] != paid_budget.sha(json.loads(row["content_json"]))):
                raise CoreError("LEGACY_OWNER_SENT_PROOF_REQUIRED", 403)
            private = policy.companion_scope == "owner_private_v1"
            saved = _meta(store, ("private_life_delivery:" if private else "live_text_reply:")
                           + row["message_id"])
            if not saved or type(saved.get("privacy_epoch")) is not int or not (
                0 <= saved["privacy_epoch"] <= epoch(store)
            ):
                raise CoreError("LEGACY_OWNER_SENT_PROOF_REQUIRED", 403)
            requests = [entry["request_id"] for entry in usage]
            expected = {"message_id": row["message_id"], "event_id": event["event_id"],
                        "plan_sha256": policy.plan_sha256, "privacy_epoch": saved["privacy_epoch"]}
            if private:
                expected.update(event_sha256=event_digest(event),
                    content_sha256=row["content_sha256"], delivery_key=row["delivery_key"],
                    action_type=row["action_type"], request_ids=requests)
                if any(not settled_usage_unchanged(store, request_id, policy.plan_sha256)
                       for request_id in requests):
                    raise CoreError("LEGACY_OWNER_SETTLED_REPLY_REQUIRED", 403)
            else:
                expected.update(job_id=job["job_id"],
                    reply_sha256=paid_budget.sha(json.loads(row["content_json"])["text"]))
                expected.update({"request_ids": requests} if policy.memory_scope == "approved_facts"
                                or len(requests) > 1 else {"request_id": requests[0]})
                if group["segmented"]:
                    expected["reply_segments"] = envelopes[row["message_id"]]
            if saved != expected:
                raise CoreError("LEGACY_OWNER_SENT_PROOF_REQUIRED", 403)
