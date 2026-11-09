"""Bounded metadata over native decisions and receipts; never replay cognition."""

import json
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from companion.contracts import CoreError
from companion.memory_context import MemorySources, check_version, read_version
from companion.memory_projections import MemoryProjections
from companion.privacy import redact, sensitive

KINDS = {
    "wake": ("wake_decisions", "decision_id"),
    "life": ("life_activity_results", "job_id"),
    "goal": ("goals", "goal_id"),
    "diary": ("diary_runs", "run_id"),
    "reflection": ("reflection_runs", "run_id"),
    "nightly": ("consolidation_runs", "run_id"),
    "model": ("model_usage", "request_id"),
    "rem": ("rem_records", "rem_id"),
}
STATES = frozenset(
    "PENDING SHADOW SKIPPED DEFERRED INVALIDATED CANDIDATE ACCEPTED QUEUED "
    "RUNNING DONE WAITING WAITING_INPUT WAITING_RESOURCE WAITING_DECISION "
    "RETRY_WAIT FAILED "
    "CANCELLED EXECUTION_UNCERTAIN COMMITTED SCHEDULED LEASED PARTIAL COMPLETE "
    "RESERVED SUCCEEDED UNKNOWN SETTLED".split()
)
REASONS = frozenset(
    "NO_CANDIDATE PAUSED QUIET_HOURS OWNER_REQUESTED_PAUSE RESOURCE_UNAVAILABLE "
    "DAILY_LIMIT COOLDOWN GOAL_DUE DISCOVERY_DUE PRIVACY_CHANGED "
    "OWNER_AUTHORITY_DISABLED OWNER_AUTHORITY_EXPIRED".split()
)


def symbol(value, allowed):
    return value if value in allowed else "UNKNOWN" if value else "NOT_RECORDED"


def object_value(value):
    try:
        result = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def measurement(value):
    if not value:
        return {"state": "NOT_RECORDED"}
    try:
        if not isinstance(value, str) or len(value) > 16384:
            raise ValueError
        receipt = json.loads(value)
        if not isinstance(receipt, dict):
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        return {"state": "INCONSISTENT"}
    if not receipt:
        return {"state": "NOT_RECORDED"}
    kind = receipt.get("count_kind")
    result = {
        "state": "RECORDED",
        "count_kind": symbol(
            kind if isinstance(kind, str) else None,
            {"exact_local_render", "provider_count", "estimate", "unsupported"},
        ),
    }
    for key in ("input_tokens", "reservation_margin", "output_allowance"):
        number = receipt.get(key)
        result[key] = number if type(number) is int and number >= 0 else None
    return result


def observed_usage(row, admission):
    """Distinguish valid paid usage from demo, reservations and proven zero I/O."""
    return bool(
        row["state"] == "SETTLED"
        and admission
        and admission["io_started_at"]
        and all(type(row[key]) is int and row[key] > 0 for key in ("input_tokens", "output_tokens"))
        and type(row["cached_input_tokens"]) is int
        and 0 <= row["cached_input_tokens"] <= row["input_tokens"]
    )


def cache_usage(store, row, admission):
    """Project cache usage only from one original, consistent usage diagnostic."""
    unknown = {"state": "NOT_RECORDED", "cached_input_tokens": None}
    if not observed_usage(row, admission):
        return unknown
    records = store.rows(
        "SELECT safe_detail_json FROM audit_log WHERE action = 'model.usage_diagnostic' AND "
        "entity_id = %s LIMIT 2",
        (row["request_id"],),
    )
    if len(records) != 1:
        return unknown

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError

    try:
        raw = records[0]["safe_detail_json"]
        if not isinstance(raw, str) or len(raw) > 16384:
            return unknown
        receipt = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
        if (
            not isinstance(receipt, dict)
            or receipt.get("cache_usage_present") is not True
            or receipt.get("actual_usage_known") is not True
        ):
            return unknown
        for key, column in (
            ("input_actual_tokens", "input_tokens"),
            ("output_actual_tokens", "output_tokens"),
            ("cached_input_tokens", "cached_input_tokens"),
            ("usage_priced_micro_usd", "billed_micro_usd"),
        ):
            if type(receipt.get(key)) is not int or receipt[key] != row[column]:
                return unknown
    except (ValueError, TypeError, RecursionError):
        return unknown
    return {"state": "RECORDED", "cached_input_tokens": row["cached_input_tokens"]}


def decision_reason(row, settings):
    result = {"kind": "decision_reason", "state": "NOT_RECORDED"}
    reason = row.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return result
    # Require the original structured proposal and persisted projection to agree.
    if object_value(row.get("proposal_json")).get("reason") != reason:
        return {**result, "state": "UNKNOWN"}
    if redact(reason, settings) != reason or sensitive(reason):
        return {**result, "state": "REDACTED"}
    return {**result, "state": "RECORDED", "text": reason[:160], "truncated": len(reason) > 160}


def provider_reasoning(core, row, roots):
    """Read one original SDK response under the caller's source and privacy fences."""
    from pydantic_ai.conversation import ConversationTypeAdapter
    from pydantic_ai.messages import ModelResponse, ThinkingPart

    from companion.model import MAX_REASONING_CHARS

    missing = {"state": "NOT_RECORDED"}
    if row["scenario"] != "chat" or not roots:
        return missing
    saved = core.store.one(
        "SELECT conversation_json FROM agent_conversations WHERE job_id = %s", (row["job_id"],)
    )
    if not saved or not saved["conversation_json"]:
        return missing
    try:
        conversation = ConversationTypeAdapter.validate_json(saved["conversation_json"])
        if conversation.conversation_id != row["job_id"]:
            return {"state": "INCONSISTENT"}
        responses = [
            message
            for message in conversation.messages
            if isinstance(message, ModelResponse)
            and (message.metadata or {}).get("core_request_id") == row["request_id"]
        ]
        if not responses:
            return missing
        if len(responses) != 1:
            return {"state": "INCONSISTENT"}
        response = responses[0]
        diagnostic = (response.provider_details or {}).get("reasoning") or {}
        text = "".join(part.content for part in response.parts if isinstance(part, ThinkingPart))
        if not text and diagnostic.get("state") == "NOT_RETURNED":
            return {"state": "NOT_RETURNED"}
        if diagnostic.get("state") != "RECORDED" or not text or len(text) > MAX_REASONING_CHARS:
            return missing
        cleaned = redact(text, core.settings)
        return {
            "state": "RECORDED",
            "text": cleaned,
            "truncated": diagnostic.get("truncated") is True,
            "redacted": diagnostic.get("redacted") is True or cleaned != text,
        }
    except (ValueError, TypeError, AttributeError, RecursionError):
        return {"state": "INCONSISTENT"}


class CognitionRead:
    def __init__(self, core):
        self.core, self.store = core, core.store
        self.projections = MemoryProjections(core.memory)

    def activity_snapshot(self, selection):
        from companion.activity_projection import snapshot

        return snapshot(self, selection)

    def page(self, kind, *, limit=30, before=None, record_id=None):
        if kind not in KINDS or type(limit) is not int or not 1 <= limit <= 50:
            raise CoreError("INVALID_COGNITION_SELECTION", 400)
        # HTTP owner authentication is mandatory at registration; runtime feature
        # switches do not revoke the owner's ability to inspect stopped work.
        version = read_version(self.core.memory)
        now = self.core.clock()
        sources = MemorySources(self.core.memory, now)
        table, key = KINDS[kind]
        if record_id is not None and before is not None:
            raise CoreError("INVALID_COGNITION_SELECTION", 400)
        cursor = None
        if before is not None:
            cursor = self.store.one(
                f'SELECT record_sequence AS position FROM {table} WHERE {key}=%s', (before,)
            )
            if cursor is None:
                raise CoreError("COGNITION_CURSOR_UNAVAILABLE", 409)
        rows = self.store.rows(
            (
                (
                    'SELECT record_sequence AS position,* FROM '
                    f'{table}'
                    ' WHERE (CAST(%s AS BIGINT) IS NULL OR '
                    'record_sequence<%s) AND (CAST(%s AS TEXT) IS NULL OR '
                    f'{key}'
                    '=%s) ORDER BY record_sequence DESC LIMIT %s'
                )
            ),
            (
                cursor and cursor["position"],
                cursor and cursor["position"],
                record_id,
                record_id,
                limit + 1,
            ),
        )
        items = []
        for row in rows[:limit]:
            item = self._record(kind, row, sources)
            if item is not None:
                # Native record time, never an identifier or the time of this read.
                item["occurred_at"] = row["occurred_at" if kind == "model" else "created_at"]
                items.append(item)
        check_version(self.core.memory, version)
        return {
            "kind": kind,
            "items": items,
            "as_of": now,
            "privacy_epoch": version[0],
            "next_cursor": rows[limit - 1][key] if len(rows) > limit else None,
            "truncated": len(rows) > limit,
            "reasoning": "EXPOSED"
            if any(item.get("reasoning", {}).get("state") == "RECORDED" for item in items)
            else "NOT_EXPOSED",
            "candidate_set": "NOT_RECORDED",
            "record_status": "RECORDED" if items else "NOT_RECORDED",
        }

    def _roots(self, kind, row):
        if kind == "wake":
            roots = [row["trigger_event_id"]] if row["trigger_event_id"] else []
            if row["goal_id"]:
                goal = self.store.one(
                    "SELECT goals.goal_id AS goal_id, goals.kind AS kind, "
                    "goals.topic AS topic, goals.scope_json AS scope_json, goals.proposal_key AS "
                    "proposal_key, goals.state AS state, goals.priority AS priority, "
                    "goals.next_due_at AS next_due_at, goals.last_error_code AS last_error_code, "
                    "goals.version AS version, goals.result_event_id AS result_event_id, "
                    "goals.created_at AS created_at, goals.updated_at AS updated_at, "
                    "goals.redacted AS redacted, goals.reflection_suggestion_id AS "
                    "reflection_suggestion_id, goals.authority_json AS authority_json FROM goals "
                    "AS goals WHERE goals.goal_id = %s AND goals.redacted = 0",
                    (row["goal_id"],),
                )
                if goal is None:
                    return [None]
                evidence = self._roots("goal", goal)
                if not evidence:
                    return [None]
                roots.extend(evidence)
            return list(dict.fromkeys(roots))
        if kind == "life":
            # Topic sources may evolve after a decision; only the saved source
            # identities describe that decision's original candidate.
            return list(object_value(row["source_fingerprints_json"]))
        elif kind == "goal":
            query, identifier = "goal_evidence WHERE goal_id=%s", row["goal_id"]
        elif kind == "diary":
            query = (
                "diary_evidence JOIN diary_entries USING(entry_id) WHERE"
                " run_id=%s AND status='COMMITTED'"
            )
            identifier = row["run_id"]
        elif kind in {"reflection", "nightly"}:
            query = (
                "reflection_evidence" if kind == "reflection" else "consolidation_evidence"
            ) + " WHERE run_id=%s"
            identifier = row["run_id"]
        else:
            query, identifier = "job_sources WHERE job_id=%s", row["job_id"]
        return [
            r["event_id"]
            for r in self.store.rows(
                "SELECT DISTINCT event_id FROM " + query + " ORDER BY event_id LIMIT 101",
                (identifier,),
            )
        ]

    def _sources_visible(self, row, roots, sources):
        return (not row.get("redacted") and len(roots) <= 100 and all(
            key and self.projections._goal_source(key, sources) for key in roots
        ))

    def _model_usage_rows(self, lower, upper):
        query = (
            'SELECT m.request_id,m.job_id,m.state,m.input_tokens,m.cached_input_tok'
            'ens,m.output_tokens,m.billed_micro_usd,m.occurred_at,a.io_started_at '
            'FROM model_usage m LEFT JOIN model_admissions a ON '
            'a.request_id=m.request_id WHERE m.occurred_at>=%s AND m.occurred_at<%s'
        )
        after, params = "", (lower, upper)
        while True:
            rows = self.store.rows(query + after + " ORDER BY m.request_id LIMIT 256", params)
            yield from rows
            if len(rows) < 256:
                return
            after, params = ' AND m.request_id>%s', (lower, upper, rows[-1]["request_id"])

    def daily_model_usage(self, local_day, timezone, now):
        """Count visible calls without loading prompts, reasoning or context measurements."""
        day, zone = date.fromisoformat(local_day), ZoneInfo(timezone)
        start = datetime.combine(day, time.min, zone).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
        # Stored ISO timestamps can carry different offsets. Narrow by calendar date
        # before testing exact instants, including both sides of a local DST boundary.
        lower = (start.date() - timedelta(days=1)).isoformat()
        upper = (end.date() + timedelta(days=2)).isoformat()
        result = dict(calls=0, observed_calls=0, cache_calls=0, uncertain_calls=0,
                      cached=0, fresh=0, unknown_input=0, output=0, billed_micro_usd=0,
                      complete=True)
        version = read_version(self.core.memory)
        sources = MemorySources(self.core.memory, now)
        for row in self._model_usage_rows(lower, upper):
            try:
                occurred = datetime.fromisoformat(row["occurred_at"])
                in_day = start <= occurred < end
            except (TypeError, ValueError):
                in_day = None
            if in_day is False:
                continue
            if not self._sources_visible(row, self._roots("model", row), sources):
                continue
            if in_day is None:
                result["complete"] = False
                continue
            result["calls"] += 1
            result["uncertain_calls"] += row["state"] in {"RESERVED", "UNKNOWN"}
            result["billed_micro_usd"] += max(0, row["billed_micro_usd"] or 0)
            if not observed_usage(row, row):
                continue
            result["observed_calls"] += 1
            result["output"] += row["output_tokens"]
            cache = cache_usage(self.store, row, row)
            if cache["state"] == "RECORDED":
                result["cache_calls"] += 1
                result["cached"] += cache["cached_input_tokens"]
                result["fresh"] += row["input_tokens"] - cache["cached_input_tokens"]
            else:
                result["unknown_input"] += row["input_tokens"]
        check_version(self.core.memory, version)
        return result

    def _record(self, kind, row, sources):
        if kind == "rem":
            from companion.cognition_rem import record

            return record(self.core, row, sources.now)
        roots = self._roots(kind, row)
        if not self._sources_visible(row, roots, sources):
            return None
        # Execution leases require an equal captured epoch; historical observation
        # requires current source custody instead. Ordinary memory writes also bump
        # the epoch and do not erase independently valid completed records.
        if kind in {"goal", "life"} and not roots:
            return None
        if kind == "diary" and row["state"] == "COMMITTED" and not roots:
            return None
        if kind == "life":
            from companion.life_topics import _source

            fingerprints = object_value(row["source_fingerprints_json"])
            try:
                if any(
                    _source(self.store, self.core.settings, key, sources.now)["source_sha256"]
                    != fingerprints[key]
                    for key in roots
                ):
                    return None
            except CoreError:
                return None
        key = KINDS[kind][1]
        item = {
            "id": row[key],
            "state": symbol(row.get("state"), STATES),
            "source_event_ids": roots,
            "source_evidence": "RECORDED" if roots else "NOT_RECORDED",
            "reason_code": symbol(row.get("reason_code") or row.get("last_error_code"), REASONS),
        }
        if kind == "wake":
            item["choice"] = symbol(row["decision"], {"SKIP", "EXPLORE", "OBSERVE"})
            item["state"] = "RECORDED"
            item["goal_id"] = row["goal_id"]
        elif kind == "life":
            item.update(
                topic_id=row["topic_id"],
                choice=symbol(
                    row["action"], {"SKIP", "CONTINUE", "EXPLORE", "FOLLOW_UP", "SHARE", "DEFER"}
                ),
                reason_code="NOT_RECORDED",
            )
            item["decision_reason"] = decision_reason(row, self.core.settings)
            item["destination"] = symbol(
                row["destination"],
                {"NONE", "PRIVATE_DIARY", "PRIVATE_MESSAGE_DRAFT", "QZONE_DRAFT"},
            )
            item["candidate_reference"] = {
                "topic_id": row["topic_id"],
                "topic_revision": row["topic_revision"],
                "source_event_ids": roots,
                "source_snapshot_sha256": row["source_sha256"],
                "authority_sha256": row["authority_sha256"],
                "allowed_items": {"state": "NOT_RECORDED"},
            }
            job = self.store.one(
                "SELECT job_id, state FROM jobs WHERE job_id = %s", (row["job_id"],)
            )
            item["job"] = (
                {"job_id": job["job_id"], "state": symbol(job["state"], STATES)}
                if job
                else {"state": "NOT_RECORDED"}
            )
        elif kind == "goal":
            from companion.cognition_goal import record

            projected = record(self, row, sources)
            if projected is None:
                return None
            item.update(projected)
        elif kind == "nightly":
            steps = object_value(row["steps_json"])
            item["steps"] = {
                name: symbol(steps.get(name), STATES)
                for name in ("n1", "n2", "n3", "memory", "diary", "reflection", "goals")
            }
        elif kind == "model":
            item["usage"] = {
                key: row[key]
                for key in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "billed_micro_usd",
                    "is_estimate",
                )
            }
            admission = self.store.one(
                "SELECT measurement_json, io_started_at FROM model_admissions WHERE request_id = "
                "%s",
                (row["request_id"],),
            )
            item["measurement"] = measurement(admission and admission["measurement_json"])
            item["usage"]["observed"] = observed_usage(row, admission)
            item["usage"]["cache_usage"] = cache_usage(self.store, row, admission)
            item["context_regions"] = item["trimming_reason"] = "NOT_RECORDED"
            item["job_id"] = row["job_id"]
            item["reasoning"] = provider_reasoning(self.core, row, roots)
        if kind in {"diary", "reflection"}:
            item["model_request_id"] = row["model_request_id"]
        return item
