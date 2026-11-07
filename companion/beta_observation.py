"""Private, content-free trace inspection; never authorizes or repairs runtime state."""

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime

from psycopg import Error as PostgresError
from psycopg_pool import PoolTimeout

from companion.cognition_read import cache_usage, measurement, observed_usage
from companion.contracts import CoreError
from companion.ops_status import read_view, schema_readiness
from companion.paid_budget import sha
from companion.privacy import epoch
from companion.transport import reply_group


def _hash(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def _digest(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) else None


def _number(value):
    return value if type(value) is int and 0 <= value < 2**63 else None


def _state(value, allowed):
    return value if value in allowed else "UNKNOWN"


def _utc(value):
    try:
        when = datetime.fromisoformat(value)
        return when.astimezone(UTC).isoformat() if when.tzinfo else None
    except (TypeError, ValueError):
        return None


def _context(view, job_id, current_epoch):
    row = view.one('SELECT value FROM runtime_meta WHERE key = %s',
                   ("live_text_context:" + job_id,))
    if row is None:
        return _native_context(view, job_id, current_epoch)
    if len(row["value"]) > 131072:
        raise ValueError("CONTEXT_TOO_LARGE")
    value = json.loads(row["value"])
    epoch = _number(value.get("privacy_epoch"))
    if epoch is None or epoch != current_epoch:
        return {"state": "STALE_EPOCH", "source_validity": "NOT_REVALIDATED",
                "privacy_epoch": epoch}
    sources, memories = value.get("sources", []), value.get("memory_sources", [])
    images = value.get("image_sources", [])
    if not isinstance(sources, list) or len(sources) > 10:
        raise ValueError("CONTEXT_SOURCES_INVALID")
    if not isinstance(memories, list) or len(memories) > 6:
        raise ValueError("CONTEXT_MEMORIES_INVALID")
    if not isinstance(images, list) or len(images) > 1:
        raise ValueError("CONTEXT_IMAGES_INVALID")
    return {
        "state": "RECORDED" if epoch is not None and epoch == current_epoch else "STALE_EPOCH",
        "source_validity": "NOT_REVALIDATED",
        "privacy_epoch": epoch,
        "plan_sha256": _digest(value.get("plan_sha256")),
        "payload_sha256": _digest(value.get("payload_sha256")),
        "history": [{"event_ref_sha256": _hash(source["event_id"]),
                     "producer": "qq.onebot", "subject": "owner_private_turn"}
                    for source in sources],
        "memory": [{"memory_ref_sha256": _hash(source["memory_id"]),
                    "subject": _state(source.get("subject"), {"user", "project"}),
                    "evidence_sha256": _digest(source.get("evidence_sha256"))}
                   for source in memories],
        "images": [{
            "producer": "observation.qq_image", "subject": "owner_shared_image",
            "source_validity": "NOT_REVALIDATED",
            "privacy_epoch": _number(source["image"].get("privacy_epoch")),
            "event_ref_sha256": _hash(source["image"]["event_id"]),
            "item_ref_sha256": _hash(source["image"]["item_id"]),
            "source_sha256": _digest(source["image"]["source"].get("source_sha256")),
            "media_sha256": _digest(source["image"].get("media_sha256")),
            "payload_sha256": _digest(source["image"].get("payload_sha256")),
            **{key: _digest(source.get(key)) for key in (
                "item_sha256", "observation_sha256", "admission_sha256",
            )},
        } for source in images],
    }


def _native_context(view, job_id, current_epoch):
    from companion.context_planner import check_bound

    result = {"state": "NOT_RECORDED", "source_validity": "NOT_REVALIDATED"}
    row = view.one('SELECT snapshots_json FROM agent_conversations WHERE job_id = %s', (job_id,))
    if row is None:
        return result
    try:
        if len(row["snapshots_json"]) > 1048576:
            raise ValueError
        snapshots = json.loads(row["snapshots_json"])
        entries = [item for item in snapshots if item["kind"] == "context_plan"]
        if len(entries) == 1:
            metadata = entries[0]["metadata"]["core_context"]
            if metadata.get("version") in {1, 2} and metadata.get("privacy_epoch") != current_epoch:
                return {**result, "state": "STALE_EPOCH"}
        job = view.one('SELECT job_id, authority_json FROM jobs WHERE job_id = %s', (job_id,))
        plan = check_bound(view, job, snapshots, current_epoch)
        if plan is None:
            return result
        def project(record):
            return {"state": record["state"],
                    "request_ref_sha256": (_hash(record["request_id"])
                                           if record["request_id"] else None),
                    "payload_sha256": record["payload_sha256"],
                    "measurement_sha256": record["measurement_sha256"],
                    "selected_group_count": len(record["selected"]),
                    "dropped": record["dropped"]}
        return {**result, "state": "RECORDED", "privacy_epoch": plan["privacy_epoch"],
                "assembly_dropped": plan.get("assembly_dropped", []),
                "plan_sha256": sha(plan), "payload_sha256": (
                    plan["requests"][-1]["payload_sha256"] if plan["requests"] else None),
                "groups": dict(Counter(group["kind"] for group in plan["groups"])),
                "requests": [project(record) for record in plan["requests"]],
                "pending": project(plan["pending"]) if plan["pending"] else None}
    except (CoreError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return {**result, "state": "INCONSISTENT"}


def _memory_selection(view, job_id, event_id, current_epoch):
    """Project persisted decisions only; never rerun retrieval or infer the model's reason."""
    result = {"state": "NOT_RECORDED", "source_validity": "NOT_REVALIDATED"}
    row = view.one('SELECT value FROM runtime_meta WHERE key = %s', ("live_text_memory:" + job_id,))
    if row is None:
        return result
    try:
        if len(row["value"]) > 131072:
            raise ValueError
        value = json.loads(row["value"])
        if _number(value.get("privacy_epoch")) != current_epoch:
            return {**result, "state": "STALE_EPOCH"}
        candidates, selected = value["candidates"], value["selected"]
        history = value["history_sources"]
        if (value["event_id"] != event_id or not _digest(value["plan_sha256"])
            or not isinstance(candidates, list) or len(candidates) > 6
            or not isinstance(history, list) or len(history) > 10
            or sha(candidates) != value["candidates_sha256"]
            or sha(history) != value["history_sources_sha256"]
            or any(not isinstance(s["memory_id"], str) or not 1 <= len(s["memory_id"]) <= 256
                   for s in candidates)
            or len({s["memory_id"] for s in candidates}) != len(candidates)
            or bool(candidates) != bool(_digest(value["review_payload_sha256"]))):
            raise ValueError
        request = value["review_request_id"]
        if selected is None:
            if not candidates or request is not None or value["selected_sha256"] is not None:
                raise ValueError
            state = "REVIEW_PENDING"
        else:
            if (not isinstance(selected, list) or len(selected) > len(candidates)
                or sha(selected) != value["selected_sha256"]
                or any(s not in candidates for s in selected)
                or len({s["memory_id"] for s in selected}) != len(selected)):
                raise ValueError
            state = "SELECTION_RECORDED" if candidates else "NO_CANDIDATES"
            if candidates:
                if not isinstance(request, str) or not 1 <= len(request) <= 256:
                    raise ValueError
                usage = view.one(
                    'SELECT m.state, m.scenario, a.payload_sha256 FROM model_usage AS m LEFT JOIN '
                    'model_admissions AS a USING (request_id) WHERE m.request_id = %s AND m.job_id '
                    '= %s', (request, job_id),
                )
                if (not usage or usage["state"] != "SETTLED"
                    or usage["scenario"] != "memory_review"
                    or usage["payload_sha256"] != value["review_payload_sha256"]):
                    raise ValueError
            elif request is not None:
                raise ValueError
        context = view.one('SELECT value FROM runtime_meta WHERE key = %s',
                           ("live_text_context:" + job_id,))
        frozen = None
        if context is not None:
            if len(context["value"]) > 131072:
                raise ValueError
            context = json.loads(context["value"])
            frozen = context["memory_sources"]
            if (context["event_id"] != event_id or context["privacy_epoch"] != current_epoch
                or context["plan_sha256"] != value["plan_sha256"] or selected is None
                or not isinstance(frozen, list) or len(frozen) > len(selected)
                or any(s not in selected for s in frozen)
                or len({s["memory_id"] for s in frozen}) != len(frozen)):
                raise ValueError
        return {
            **result, "state": state, "candidate_count": len(candidates),
            "selected_count": len(selected) if selected is not None else None,
            "frozen_context_count": len(frozen) if frozen is not None else None,
            "selection_reason": "NOT_RECORDED", "discarded_history": "NOT_RECORDED",
            "candidates": [{
                "memory_ref_sha256": _hash(source["memory_id"]),
                "disposition": (
                    "REVIEW_PENDING" if selected is None else
                    "NOT_SELECTED_BY_RECORDED_REVIEW" if source not in selected else
                    "CHAT_CONTEXT_NOT_RECORDED" if frozen is None else
                    "IN_FROZEN_CHAT_CONTEXT" if source in frozen else "NOT_IN_FROZEN_CHAT_CONTEXT"
                ),
            } for source in candidates],
        }
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return {**result, "state": "INCONSISTENT"}


def _recorded_window(view, event_id, current_epoch):
    """Bind an event to its persisted window, independently of today's configuration."""
    result = {"state": "NOT_RECORDED", "source_validity": "NOT_REVALIDATED"}
    row = view.one('SELECT value FROM runtime_meta WHERE key = '
        '%s', ("live_text_input:" + event_id,))
    if row is None:
        return result
    try:
        if len(row["value"]) > 65536:
            raise ValueError
        value = json.loads(row["value"])
        if _number(value.get("privacy_epoch")) != current_epoch:
            return {**result, "state": "STALE_EPOCH"}
        plan = _digest(value["window_sha256"])
        if not plan or value["event_id"] != event_id:
            raise ValueError
        history = view.one('SELECT value FROM runtime_meta WHERE key = %s',
                           ("live_text_window:" + plan,))
        if history is None or len(history["value"]) > 131072:
            raise ValueError
        history = json.loads(history["value"])
        approval, binding = history["approval"], history["binding"]
        if (history["state"] not in {"OPEN", "STOPPED", "RETIRED"}
            or approval["plan_sha256"] != plan or binding["plan_sha256"] != plan
            or binding["approval_sha256"] != sha(approval)
            or binding["privacy_epoch"] != current_epoch
            or not _digest(approval["approval_sha256"])
            or not re.fullmatch(r"[a-f0-9]{40}", approval["candidate_sha"])
            or approval["execution"] not in {"local_synthetic", "approved_live"}):
            raise ValueError
        return {**result, "state": "RECORDED", "plan_sha256": plan,
                **{key: approval[key] for key in ("approval_sha256", "candidate_sha", "execution")}}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return {**result, "state": "INCONSISTENT"}


def _reply_groups(view, event_id, redacted):
    if redacted:
        return []
    heads = view.rows(
        'SELECT MIN(message_id) AS message_id FROM outbox WHERE origin_event_id = %s AND '
        "action_type = 'qq.private.reply' AND NOT reply_group_id IS NULL GROUP BY reply_group_id "
        'ORDER BY reply_group_id NULLS FIRST LIMIT 21', (event_id,),
    )
    if len(heads) > 20:
        raise ValueError("TRACE_REPLY_GROUP_LIMIT")
    groups = []
    for head in heads:
        try:
            group = reply_group(view, head["message_id"])
        except CoreError:
            continue  # A damaged or erased manifest cannot support a larger expected count.
        if not group["segmented"]:
            continue
        rows = group["rows"]
        states = dict(Counter(row["state"] for row in rows))
        groups.append({
            "state": "RECORDED", "job_ref_sha256": _hash(rows[0]["reply_group_id"]),
            "reply_group_sha256": rows[0]["reply_group_sha256"],
            "expected_reply_count": len(rows), "outbox": states,
            "all_parts_sent": states.get("SENT", 0) == len(rows),
        })
    return groups


def _trace(view, trace_id):
    events = view.rows(
        'SELECT e.event_id, e.redacted, e.occurred_at FROM events AS e JOIN conversations AS c '
        "USING (conversation_id) WHERE e.trace_id = %s AND e.actor_id = 'owner' AND "
        "e.privacy_scope = 'private' AND e.source = 'qq.onebot' AND c.scope = 'private' AND "
        "c.owner_actor_id = 'owner' LIMIT 2",
        (trace_id,),
    )
    if len(events) != 1:
        return {"state": "NOT_FOUND" if not events else "AMBIGUOUS_TRACE"}
    event = events[0]
    current_epoch = epoch(view)
    jobs = view.rows('SELECT job_id, state, attempts, last_error_code FROM jobs WHERE event_id = '
        "%s AND kind = 'chat' LIMIT 21", (event["event_id"],))
    if len(jobs) > 20:
        raise ValueError("TRACE_JOB_LIMIT")
    inspected = []
    for job in jobs:
        usage = view.rows(
            'SELECT m.request_id, m.state, m.scenario, m.input_tokens, m.cached_input_tokens, '
            'm.output_tokens, a.logical_request_id, m.billed_micro_usd, m.pricing_known, '
            'a.io_started_at, a.attempt_number, a.reserved_micro_usd, a.measurement_json FROM '
            'model_usage AS m LEFT JOIN model_admissions AS a USING (request_id) WHERE m.job_id = '
            '%s ORDER BY m.record_sequence NULLS FIRST LIMIT 101', (job["job_id"],),
        )
        if len(usage) > 100:
            raise ValueError("TRACE_USAGE_LIMIT")
        calls = []
        for row in usage:
            # RESERVED/UNKNOWN tokens may be estimates. Demo calls are never real usage proof.
            actual = observed_usage(row, row)
            cache = cache_usage(view, row, row)
            calls.append({
                "request_ref_sha256": _hash(row["request_id"]),
                "logical_request_ref_sha256": (
                    _hash(row["logical_request_id"]) if row["logical_request_id"] else None
                ),
                "state": _state(row["state"], {"RESERVED", "SETTLED", "UNKNOWN"}),
                "scenario": _state(row["scenario"], {"chat", "memory_review", "content_vision"}),
                "paid_dispatch_recorded": row["io_started_at"] is not None,
                "attempt_number": _number(row["attempt_number"]),
                "prompt_tokens": _number(row["input_tokens"]) if actual else None,
                "prompt_cache_hit_tokens": cache["cached_input_tokens"],
                "cache_usage": cache,
                "completion_tokens": _number(row["output_tokens"]) if actual else None,
                "configured_price_micro_usd": (
                    _number(row["billed_micro_usd"]) if actual and row["pricing_known"] else None
                ),
                "accounted_or_reserved_micro_usd": _number(row["billed_micro_usd"]),
                "reservation_micro_usd": _number(row["reserved_micro_usd"]),
                "measurement": measurement(row["measurement_json"]),
                "context_regions": "NOT_RECORDED", "trimming_reason": "NOT_RECORDED",
            })
        inspected.append({
            "job_ref_sha256": _hash(job["job_id"]),
            "state": _state(job["state"], {
                "QUEUED", "RUNNING", "RETRY_WAIT", "SUCCEEDED", "FAILED", "CANCELLED",
            }),
            "worker_attempts": _number(job["attempts"]),
            "failure_recorded": job["last_error_code"] is not None,
            "failure_code_sha256": (_hash(job["last_error_code"])
                                    if job["last_error_code"] else None),
            "context": ({"state": "REDACTED", "source_validity": "NOT_REVALIDATED"}
                        if event["redacted"] else _context(view, job["job_id"], current_epoch)),
            "memory_selection": (
                {"state": "REDACTED", "source_validity": "NOT_REVALIDATED"}
                if event["redacted"] else
                _memory_selection(view, job["job_id"], event["event_id"], current_epoch)
            ),
            "model_attempt_count": len(calls), "model_calls": calls,
        })
    deliveries = view.rows(
        'SELECT state, COUNT(*) AS count FROM outbox WHERE origin_event_id = %s AND action_type = '
        "'qq.private.reply' GROUP BY state", (event["event_id"],),
    )
    states = {row["state"]: row["count"] for row in deliveries}
    return {"state": "REDACTED" if event["redacted"] else "RECORDED",
            "producer": "qq.onebot", "subject": "owner_private_input",
            "event_occurred_at_utc": None if event["redacted"] else _utc(event["occurred_at"]),
            "event_ref_sha256": _hash(event["event_id"]), "privacy_epoch": current_epoch,
            "recorded_window": (
                {"state": "REDACTED", "source_validity": "NOT_REVALIDATED"}
                if event["redacted"] else _recorded_window(view, event["event_id"], current_epoch)
            ),
            "jobs": inspected,
            "reply_groups": _reply_groups(view, event["event_id"], event["redacted"]),
            "outbox": {state: states[state] for state in (
                "PENDING", "LEASED", "SENDING", "RETRY_WAIT", "SENT", "DELIVERY_UNCERTAIN",
                "FAILED", "CANCELLED",
            ) if state in states},
            "phone_received": None, "phone_e2e_ms": None, "provider_invoice_micro_usd": None}


def inspect_context(settings, trace_id):
    """Read one trace in one bounded transaction. Local config pins are labelled as such.

    This does not validate platform evidence, frozen sources or independent journals;
    their runtime guards retain authority. No plaintext, paths or credentials leave it.
    """
    if not isinstance(trace_id, str) or not 1 <= len(trace_id) <= 256:
        raise ValueError("INVALID_TRACE_ID")
    approval = settings.live_text_window
    result = {
        "schema_version": 1, "observed_at": datetime.now(UTC).isoformat(),
        "read_only": True, "diagnostic_only": True, "trace_ref_sha256": _hash(trace_id),
        "configured_candidate_sha": approval.candidate_sha if approval else None,
        "runtime_image_digest": None,
        "configured_approval_sha256": approval.approval_sha256 if approval else None,
        "configured_plan_sha256": approval.plan_sha256 if approval else None,
        "configured_features": {
            name: getattr(settings, name) for name in (
                "memory_enabled", "content_enabled", "affect_enabled", "diary_enabled",
                "consolidation_enabled", "reflection_enabled", "goals_enabled",
                "wake_enabled", "exploration_enabled", "social_enabled", "proactive_enabled",
            )
        },
        "configured_scopes": {
            "history": approval.history_scope if approval else "none",
            "memory": approval.memory_scope if approval else "none",
            "rich_input": getattr(approval, "rich_input_scope", "none"),
            "image": getattr(approval, "image_scope", "none"),
        },
        "billing_basis": "configured_rates_not_invoice", "blockers": [],
    }
    try:
        with read_view(settings) as view:
            _, blocker = schema_readiness(view)
            if blocker:
                result["blockers"].append(blocker)
                return result
            owner = view.one("SELECT value FROM runtime_meta WHERE key = 'owner_qq_id'")
            if not owner or owner["value"] != settings.owner_qq_id:
                result["blockers"].append("OWNER_IDENTITY_MISMATCH")
                return result
            result["trace"] = _trace(view, trace_id)
    except (PostgresError, PoolTimeout, OSError, ValueError, TypeError, KeyError, AttributeError):
        result["blockers"].append("TRACE_SNAPSHOT_UNAVAILABLE")
    return result
