"""On-demand owner inspection of native work; never a presence or execution authority."""

from types import SimpleNamespace
from typing import Annotated

from pydantic import Field, model_validator

from companion import capabilities, private_life
from companion.autonomous_selector import foreground_clear
from companion.contracts import CoreError, StrictModel
from companion.memory_context import MemorySources, check_version, read_version
from companion.store import after

Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=160)]


class ActivitySelection(StrictModel):
    conversation_id: Identifier
    limit: int = Field(default=20, strict=True, ge=1, le=20)
    memory_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    episode_ids: list[Identifier] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def bounded_selection(self):
        if len(self.memory_ids) + len(self.episode_ids) > 20:
            raise ValueError("Select at most 20 memories and episodes together")
        return self


class ConversationSources(MemorySources):
    """Narrow the original provenance reader to one existing private conversation."""

    def __init__(self, memory, now, conversation_id):
        super().__init__(memory, now)
        self.conversation_id = conversation_id
        self.valid_until = None

    def _lineage(self, event_id):
        rows = list(super()._lineage(event_id))
        # Native observations live in Core's internal observation conversation.
        # Their owner roots, rather than that storage container, bind this view.
        if (not any(row["conversation_id"] == self.conversation_id
                    and row["actor_id"] == "owner" for row in rows)
            or any(row["conversation_id"] != self.conversation_id
                   and row["event_type"] != "content.observed"
                   and not (row["source"] == "owner.decision"
                            and row["event_type"] == "memory.corrected") for row in rows)):
            return []
        deadlines = [after(row["ingested_at"], self.settings.raw_event_retention_days * 86400)
                     for row in rows if row["ingested_at"]]
        deadlines = [deadline for deadline in deadlines if deadline > self.now]
        if deadlines:
            self.valid_until = min([*deadlines, *([self.valid_until] if self.valid_until else [])])
        return rows


def _roots(store, table, key, identifier):
    # All identifiers here are module constants, never caller-supplied SQL.
    return [row["event_id"] for row in store.rows(
        f"SELECT event_id FROM {table} WHERE {key}=%s ORDER BY event_id LIMIT 101",
        (identifier,),
    )]


def _available(reader, sources, roots):
    return (1 <= len(roots) <= 100
            and all(reader.projections._goal_source(key, sources) for key in roots))


def _result(reader, sources, key):
    if not key or not reader.projections._goal_source(key, sources):
        return None
    row = reader.store.one(
        'SELECT o.event_id, o.result_status, o.source_sha256, e.occurred_at FROM observations AS o '
        'JOIN events AS e USING (event_id) WHERE o.event_id = %s AND o.redacted = 0 AND e.redacted '
        '= 0', (key,),
    )
    if row:
        row["meaning"] = ("MEDIA_ANALYSIS_COVERAGE" if row["result_status"] == "WATCHED"
                          else "OBSERVATION_RECEIPT")
    return row


def _candidates(store, conversation_id, limit):
    # Ordering nominates records only. Original readers still validate every source
    # and completed step. One combined cap covers jobs, goals/commitments and reads.
    return store.rows(
        'SELECT _0.kind AS kind, _0.id AS id, _0.recorded_at AS recorded_at, _0.priority AS '
        "priority FROM (SELECT 'job' AS kind, j.job_id AS id, j.created_at AS recorded_at, CASE "
        "WHEN j.state IN ('UNKNOWN', 'EXECUTION_UNCERTAIN') THEN 0 ELSE 2 END AS priority FROM "
        'jobs AS j JOIN events AS e ON j.event_id = e.event_id WHERE e.conversation_id = %s AND '
        "e.redacted = 0 UNION ALL SELECT 'goal' AS goal, g.goal_id AS goal_id, g.created_at AS "
        'created_at, CASE WHEN EXISTS(SELECT 1 AS "1" FROM goal_steps AS s WHERE s.goal_id = '
        "g.goal_id AND s.state = 'EXECUTION_UNCERTAIN') THEN 0 WHEN g.kind = 'commitment' AND "
        'EXISTS(SELECT 1 AS "1" FROM commitments AS c LEFT JOIN outbox AS o ON o.message_id = '
        "c.feedback_message_id WHERE c.goal_id = g.goal_id AND NOT c.state IN ('ERASED', "
        "'FULFILLED', 'EXPLAINED_FAILURE') AND COALESCE(o.state, '') <> 'SENT') THEN 1 ELSE 2 END "
        'AS _col_3 FROM goals AS g WHERE g.redacted = 0 AND EXISTS(SELECT 1 AS "1" FROM '
        'goal_evidence AS ge JOIN events AS e ON ge.event_id = e.event_id WHERE ge.goal_id = '
        "g.goal_id AND e.conversation_id = %s) UNION ALL SELECT 'read' AS read, r.request_id AS "
        "request_id, r.created_at AS created_at, CASE WHEN r.state = 'UNKNOWN' OR a.state = "
        "'UNKNOWN' THEN 0 ELSE 2 END AS _col_3 FROM exploration_requests AS r LEFT JOIN "
        'reading_attempts AS a ON r.request_id = a.request_id WHERE r.redacted = 0 AND r.operation '
        '= \'read\' AND EXISTS(SELECT 1 AS "1" FROM exploration_evidence AS re JOIN events AS e ON '
        're.event_id = e.event_id WHERE re.request_id = r.request_id AND e.conversation_id = %s)) '
        'AS _0 ORDER BY _0.priority NULLS FIRST, _0.recorded_at DESC NULLS LAST, _0.id NULLS FIRST '
        'LIMIT %s',
        (conversation_id, conversation_id, conversation_id, limit + 1),
    )


def _lease(row, now):
    deadline = row.get("lease_until")
    return {"until": deadline, "valid_at_as_of": bool(
        row.get("state") == "RUNNING" and row.get("lease_token") and deadline and now < deadline),
        "meaning": "RECORDED_LEASE_ONLY", "worker_presence": "UNKNOWN"}


def _bounded_steps(store, key):
    steps = store.rows('SELECT goal_steps.step_id AS step_id, goal_steps.goal_id AS goal_id, '
        'goal_steps.position AS position, goal_steps.scope_json AS scope_json, goal_steps.state AS '
        'state, goal_steps.attempts AS attempts, goal_steps.max_attempts AS max_attempts, '
        'goal_steps.lease_token AS lease_token, goal_steps.lease_until AS lease_until, '
        'goal_steps.captured_privacy_epoch AS captured_privacy_epoch, goal_steps.outcome_event_id '
        'AS outcome_event_id, goal_steps.last_error_code AS last_error_code, goal_steps.created_at '
        'AS created_at, goal_steps.updated_at AS updated_at FROM goal_steps AS goal_steps WHERE '
        'goal_steps.goal_id = %s LIMIT 4', (key,))
    return (steps, len(steps) <= 3 and all(
        len(_roots(store, "goal_step_evidence", "step_id", step["step_id"])) <= 100
        for step in steps))


def _work(reader, sources, candidate):
    from companion.cognition_read import STATES, symbol

    store, kind, key = reader.store, candidate["kind"], candidate["id"]
    if kind == "goal":
        row = store.one('SELECT goals.goal_id AS goal_id, goals.kind AS kind, goals.topic AS '
            'topic, goals.scope_json AS scope_json, goals.proposal_key AS proposal_key, '
            'goals.state AS state, goals.priority AS priority, goals.next_due_at AS next_due_at, '
            'goals.last_error_code AS last_error_code, goals.version AS version, '
            'goals.result_event_id AS result_event_id, goals.created_at AS created_at, '
            'goals.updated_at AS updated_at, goals.redacted AS redacted, '
            'goals.reflection_suggestion_id AS reflection_suggestion_id, goals.authority_json AS '
            'authority_json FROM goals AS goals WHERE goals.goal_id = %s', (key,))
        # The original cognition reader allows 256 step roots; this view's approved
        # selection is narrower. Never shorten a required source set to fit it.
        raw_steps, bounded = _bounded_steps(store, key)
        if not bounded:
            return None
        item = reader._record("goal", row, sources)
        if item is None:
            return None
        item["native_state"] = item.pop("state")
        leases = {step["step_id"]: _lease(step, sources.now) for step in raw_steps}
        for step in item["steps"]:
            step["lease"] = leases[step["step_id"]]
        commitment = store.one(
            'SELECT c.commitment_id, c.state, c.deadline, o.state AS delivery_state FROM '
            'commitments AS c LEFT JOIN outbox AS o ON o.message_id = c.feedback_message_id AND '
            "o.action_type = 'qq.private.reply' WHERE c.goal_id = %s AND c.state <> "
            "'ERASED'", (key,),
        )
        if commitment:
            commitment["told"] = commitment["delivery_state"] == "SENT"
            commitment["state"] = symbol(commitment["state"], {
                "ACKNOWLEDGED", "IN_PROGRESS", "FULFILLED", "EXPLAINED_FAILURE"})
            commitment["delivery_state"] = symbol(commitment["delivery_state"], {
                "PENDING", "LEASED", "SENDING", "SENT", "DELIVERY_UNCERTAIN",
                "CANCELLED", "FAILED", "RETRY_WAIT", "BLOCKED"})
        item.update(kind="goal", commitment=commitment,
                    result=(_result(reader, sources, row["result_event_id"])
                            if item["execution"]["state"] == "done" else None))
        return item
    if kind == "job":
        row = store.one('SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, jobs.kind AS '
            'kind, jobs.state AS state, jobs.idempotency_key AS idempotency_key, jobs.attempts AS '
            'attempts, jobs.max_attempts AS max_attempts, jobs.lease_owner AS lease_owner, '
            'jobs.lease_until AS lease_until, jobs.next_attempt_at AS next_attempt_at, '
            'jobs.last_error_code AS last_error_code, jobs.created_at AS created_at, '
            'jobs.updated_at AS updated_at, jobs.lease_token AS lease_token, jobs.context_tracked '
            'AS context_tracked, jobs.authority_json AS authority_json FROM jobs AS jobs WHERE '
            'jobs.job_id = %s', (key,))
        roots = sorted(set([row["event_id"], *_roots(store, "job_sources", "job_id", key)]))
        if not _available(reader, sources, roots):
            return None
        return {"kind": "job", "id": key, "native_state": symbol(row["state"], STATES),
                "source_event_ids": roots, "lease": _lease(row, sources.now),
                "progress": "NOT_RECORDED"}
    row = store.one('SELECT exploration_requests.request_id AS request_id, '
        'exploration_requests.request_key AS request_key, exploration_requests.adapter_id AS '
        'adapter_id, exploration_requests.operation AS operation, exploration_requests.url AS url, '
        'exploration_requests.goal_step_json AS goal_step_json, exploration_requests.state AS '
        'state, exploration_requests.lease_token AS lease_token, exploration_requests.lease_until '
        'AS lease_until, exploration_requests.captured_privacy_epoch AS captured_privacy_epoch, '
        'exploration_requests.reserved_bytes AS reserved_bytes, exploration_requests.used_bytes AS '
        'used_bytes, exploration_requests.result_sha256 AS result_sha256, '
        'exploration_requests.last_error_code AS last_error_code, exploration_requests.created_at '
        'AS created_at, exploration_requests.started_at AS started_at, '
        'exploration_requests.updated_at AS updated_at, exploration_requests.redacted AS redacted, '
        'exploration_requests.reserved_requests AS reserved_requests, '
        'exploration_requests.used_requests AS used_requests, exploration_requests.source_grant_id '
        'AS source_grant_id, exploration_requests.source_grant_epoch AS source_grant_epoch, '
        'exploration_requests.authority_json AS authority_json, exploration_requests.query AS '
        'query, exploration_requests.discovery_mode AS discovery_mode FROM exploration_requests AS '
        'exploration_requests WHERE exploration_requests.request_id = %s', (key,))
    roots = _roots(store, "exploration_evidence", "request_id", key)
    if not _available(reader, sources, roots):
        return None
    attempt = store.one('SELECT reading_attempts.request_id AS request_id, '
        'reading_attempts.adapter_id AS adapter_id, reading_attempts.lease_token AS lease_token, '
        'reading_attempts.lease_until AS lease_until, reading_attempts.context_json AS '
        'context_json, reading_attempts.allowed_hosts_json AS allowed_hosts_json, '
        'reading_attempts.original_url_sha256 AS original_url_sha256, '
        'reading_attempts.source_grant_json AS source_grant_json, reading_attempts.state AS state, '
        'reading_attempts.result_sha256 AS result_sha256, reading_attempts.result_event_ids_json '
        'AS result_event_ids_json, reading_attempts.observed_requests AS observed_requests, '
        'reading_attempts.observed_bytes AS observed_bytes, reading_attempts.created_at AS '
        'created_at, reading_attempts.updated_at AS updated_at FROM reading_attempts AS '
        'reading_attempts WHERE reading_attempts.request_id = %s', (key,))
    results = []
    if attempt:
        import json

        raw = attempt["result_event_ids_json"]
        try:
            keys = json.loads(raw) if isinstance(raw, str) and len(raw) <= 16384 else None
        except (ValueError, TypeError, RecursionError):
            keys = None
        if (not isinstance(keys, list) or len(keys) > 20
            or any(not isinstance(value, str) or not 1 <= len(value) <= 160 for value in keys)):
            return None
        results = [_result(reader, sources, value) for value in keys]
        if any(value is None for value in results):
            return None
    return {"kind": "read", "id": key, "native_state": symbol(row["state"], STATES),
            "attempt_state": symbol(attempt and attempt["state"], STATES),
            "source_event_ids": roots, "lease": _lease(attempt or row, sources.now),
            "progress": "NOT_RECORDED", "results": results}


def _attention(reader, sources, selection):
    store, projections, result = reader.store, reader.projections, []
    truncated = False
    for kind, keys in (("memory", selection.memory_ids), ("episode", selection.episode_ids)):
        for key in sorted(set(keys)):
            if kind == "memory":
                row = store.one('SELECT memories.memory_id AS memory_id, memories.subject AS '
                    'subject, memories.kind AS kind, memories.content AS content, memories.status '
                    'AS status, memories.origin AS origin, memories.confidence AS confidence, '
                    'memories.importance AS importance, memories.privacy_scope AS privacy_scope, '
                    'memories.supersedes_memory_id AS supersedes_memory_id, memories.created_at AS '
                    'created_at, memories.updated_at AS updated_at, memories.valid_from AS '
                    'valid_from, memories.valid_to AS valid_to, memories.last_reviewed_at AS '
                    'last_reviewed_at, memories.fact_key AS fact_key, memories.needs_review AS '
                    'needs_review FROM memories AS memories WHERE memories.memory_id = %s AND '
                    "memories.privacy_scope = 'private' AND memories.confidence > 0 AND "
                    "memories.status IN ('CONFIRMED', 'HYPOTHESIS')", (key,))
                roots = [r["event_id"] for r in store.rows(
                    'SELECT event_id FROM memory_evidence WHERE memory_id = %s AND polarity = 1 '
                    'ORDER BY event_id NULLS FIRST LIMIT 101', (key,))]
            else:
                row = store.one('SELECT episodes.episode_id AS episode_id, episodes.title AS '
                    'title, episodes.summary AS summary, episodes.conversation_id AS '
                    'conversation_id, episodes.privacy_scope AS privacy_scope, episodes.started_at '
                    'AS started_at, episodes.ended_at AS ended_at, episodes.status AS status, '
                    'episodes.extractor_version AS extractor_version, episodes.created_at AS '
                    'created_at FROM episodes AS episodes WHERE episodes.episode_id = %s AND '
                    "episodes.conversation_id = %s AND episodes.privacy_scope = 'private' AND "
                    "episodes.status = 'ACTIVE'",
                                (key, selection.conversation_id))
                roots = _roots(store, "episode_events", "episode_id", key)
            if (not row or kind == "episode" and len(roots) > 20
                or not _available(reader, sources, roots)
                or kind == "memory" and not sources.current(row)):
                truncated = True
                continue
            events = [sources.event(value, require_text=False) for value in roots]
            if any(event is None for event in events):
                truncated = True
                continue
            goals, cut = projections._goals(roots, sources, None)
            bounded_goals = [goal for goal in goals
                             if _bounded_steps(store, goal["goal_id"])[1]]
            cut |= len(bounded_goals) != len(goals)
            goals = bounded_goals
            temperature, reasons = projections._temperature(events, goals, set(), sources.now)
            result.append({"kind": kind, "id": key, "temperature": temperature,
                           "attention_reasons": reasons, "source_event_ids": roots,
                           "goals": [{field: goal[field] for field in (
                               "goal_id", "native_state", "state", "told", "closed")}
                                     for goal in goals], "goals_truncated": cut})
            truncated |= cut
    return {"items": result, "truncated": truncated, "used_in_request": "NOT_RECORDED"}


def snapshot(reader, selection):
    selection = ActivitySelection.model_validate(selection)
    core, store = reader.core, reader.store
    if store.in_transaction:
        raise CoreError("ACTIVITY_SNAPSHOT_BUSY", 409)
    version = read_version(core.memory)
    now = core.clock()
    # A repeatable read-only transaction pins all native rows without taking a writer
    # reservation. Recheck *after* ending it, on the latest epoch and Ledger.
    with store.read_snapshot():
        conversation = store.one('SELECT 1 FROM conversations WHERE conversation_id = %s AND scope '
            "= 'private' AND owner_actor_id = 'owner'",
                                 (selection.conversation_id,))
        if not conversation:
            raise CoreError("ACTIVITY_CONVERSATION_UNAVAILABLE", 404)
        sources = ConversationSources(core.memory, now, selection.conversation_id)
        candidates = _candidates(store, selection.conversation_id, selection.limit)
        items = []
        truncated = len(candidates) > selection.limit
        for candidate in candidates[:selection.limit]:
            item = _work(reader, sources, candidate)
            if item is None:
                truncated = True
                continue
            items.append({**item, "recorded_at": candidate["recorded_at"]})
        facts = capabilities.snapshot(store, core.settings, lambda: now)["capabilities"]
        result = {"projection_version": 1, "mode": "SHADOW", "as_of": now,
                  "privacy_epoch": version[0], "conversation_id": selection.conversation_id,
                  "items": items, "truncated": truncated,
                  "attention": _attention(reader, sources, selection),
                  "clues": {"state": "NOT_RECORDED", "items": []},
                  "capabilities": [{"id": fact["id"], "state": fact["state"],
                                    "reason_codes": fact["reason_codes"][:20],
                                    "truncated": len(fact["reason_codes"]) > 20}
                                   for fact in facts[:64]],
                  "capabilities_truncated": len(facts) > 64,
                  "foreground": {"clear": foreground_clear(SimpleNamespace(
                      store=store, settings=core.settings, clock=lambda: now)),
                                 "model_busy": private_life.busy(store, now)},
                  "presence": {"state": "UNKNOWN", "reason": "NO_TRUSTED_SCENE_SOURCE"},
                  "interruptibility": "NOT_RECORDED", "progress": "NOT_RECORDED",
                  "reasoning": "NOT_EXPOSED", "source_valid_until": sources.valid_until}
    check_version(core.memory, version)
    if sources.valid_until and core.clock() >= sources.valid_until:
        raise CoreError("PRIVACY_CHANGED", 409)
    return result
