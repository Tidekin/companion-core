"""Bounded continuation of original Goal steps; no execution or model loop."""

import json
from contextlib import nullcontext
from datetime import UTC

from companion.contracts import CoreError
from companion.goal_protocol import GoalContinuationDecision
from companion.store import after


def closure(engine, ids):
    rows = engine.store.rows(
        'WITH RECURSIVE sources(event_id) AS (SELECT value COLLATE "C" FROM '
        'JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS parsed(value) UNION SELECT '
        'd.source_event_id FROM event_dependencies AS d JOIN sources AS s ON d.event_id = '
        's.event_id) SELECT event_id FROM sources ORDER BY event_id NULLS FIRST',
        (json.dumps(sorted(set(ids))),),
    )
    keys = [row["event_id"] for row in rows]
    if not 1 <= len(keys) <= 256:
        raise CoreError("GOAL_SOURCE_LIMIT", 409)
    engine.validate_sources(keys)
    return keys


def requests(engine, *, goal_id=None, step_id=None):
    return engine.store.rows(
        "SELECT r.*,a.state AS attempt_state FROM exploration_requests r "
        "LEFT JOIN reading_attempts a USING(request_id) WHERE "
        + ((
            '(CASE WHEN r.goal_step_json IS JSON THEN '
            "(r.goal_step_json::json#>>'{goal_id}') END)=%s"
        ) if goal_id else
           (
               '(CASE WHEN r.goal_step_json IS JSON THEN '
               "(r.goal_step_json::json#>>'{step_id}') END)=%s"
           )), (goal_id or step_id,),
    )


def unknown(engine, goal_id):
    return bool(engine.store.one('SELECT 1 FROM goal_steps WHERE goal_id = %s AND state = '
        "'EXECUTION_UNCERTAIN'", (goal_id,))) or any(
        row["state"] == "UNKNOWN" or row["attempt_state"] == "UNKNOWN"
        for row in requests(engine, goal_id=goal_id))


def dispatched(engine, step_id):
    return any(row["started_at"] is not None or row["attempt_state"] is not None
               for row in requests(engine, step_id=step_id))


def plan(engine, goal_id):
    goal = engine.get(goal_id)
    engine.validate_authority(goal)
    engine.validate_sources(engine.source_ids(goal_id))
    steps = engine.store.rows('SELECT goal_steps.step_id AS step_id, goal_steps.goal_id AS '
        'goal_id, goal_steps.position AS position, goal_steps.scope_json AS scope_json, '
        'goal_steps.state AS state, goal_steps.attempts AS attempts, goal_steps.max_attempts AS '
        'max_attempts, goal_steps.lease_token AS lease_token, goal_steps.lease_until AS '
        'lease_until, goal_steps.captured_privacy_epoch AS captured_privacy_epoch, '
        'goal_steps.outcome_event_id AS outcome_event_id, goal_steps.last_error_code AS '
        'last_error_code, goal_steps.created_at AS created_at, goal_steps.updated_at AS updated_at '
        'FROM goal_steps AS goal_steps WHERE goal_steps.goal_id = %s ORDER BY goal_steps.position '
        'NULLS FIRST',
                              (goal_id,))
    uncertain = unknown(engine, goal_id)
    allowed = (goal["kind"] == "self" and json.loads(goal["scope_json"]).get("operation")
               == "read_plan" and goal["state"] == "WAITING_INPUT" and not uncertain)
    for step in steps:
        step["source_event_ids"] = engine.step_source_ids(step["step_id"])
        engine.validate_sources(step["source_event_ids"])
    actions = (["CONTINUE"] if engine.clock() >= goal["next_due_at"] else []) + ["WAIT", "ABANDON"]
    return {"goal_id": goal_id, "version": goal["version"], "kind": goal["kind"],
        "state": goal["state"], "scope": json.loads(goal["scope_json"]),
        "next_due_at": goal["next_due_at"], "unknown": uncertain,
        "steps": [{**{key: step[key] for key in (
            "step_id", "position", "state", "attempts", "last_error_code", "outcome_event_id",
            "source_event_ids")}, "scope": json.loads(step["scope_json"])} for step in steps],
        "allowed_actions": actions if allowed else []}


def continue_plan(engine, goal_id, decision, *, autonomous=False):
    if not engine.settings.goals_enabled:
        raise CoreError("GOALS_DISABLED", 403)
    if not isinstance(decision, GoalContinuationDecision):
        decision = GoalContinuationDecision.model_validate(decision)
    s, now = engine.store, engine.clock()
    with nullcontext() if s.in_transaction else s.transaction():
        goal = engine.get(goal_id)
        engine.validate_authority(goal)
        if autonomous and s.one("SELECT 1 FROM runtime_meta WHERE key = 'life_paused' AND value = "
            "'1'"):
            raise CoreError("AUTONOMY_PAUSED", 409)
        if goal["kind"] != "self" or json.loads(goal["scope_json"]).get("operation") != "read_plan":
            raise CoreError("GOAL_PLAN_REQUIRED", 403)
        if goal["version"] != decision.expected_version:
            raise CoreError("GOAL_VERSION_CHANGED", 409)
        if unknown(engine, goal_id):
            raise CoreError("GOAL_EXECUTION_UNCERTAIN", 409)
        if goal["state"] != "WAITING_INPUT" or any(
            row["state"] in {"QUEUED", "RUNNING"} for row in requests(engine, goal_id=goal_id)
        ):
            raise CoreError("GOAL_CONTINUATION_NOT_READY", 409)
        engine.validate_reflection(goal["reflection_suggestion_id"])
        sources = set(engine.source_ids(goal_id))
        sources.update(row["outcome_event_id"] for row in s.rows(
            "SELECT outcome_event_id FROM goal_steps WHERE goal_id = %s AND state = 'DONE'",
            (goal_id,)))
        evidence = closure(engine, sources)
        if decision.action == "ABANDON":
            engine.cancel(goal_id)
        elif decision.action == "WAIT":
            due = decision.wait_until.astimezone(UTC).isoformat(timespec="microseconds")
            if not now < due <= after(now, 7 * 86400):
                raise CoreError("INVALID_GOAL_WAIT", 400)
            s.execute('UPDATE goals SET next_due_at = %s, version = version + 1, updated_at = %s '
                'WHERE goal_id = %s AND version = %s', (due, now, goal_id, goal["version"]))
        else:
            if now < goal["next_due_at"]:
                raise CoreError("GOAL_WAIT_NOT_DUE", 409)
            original_ids = {row["step_id"] for row in s.rows(
                'SELECT step_id FROM goal_steps WHERE goal_id = %s', (goal_id,))}
            pending = s.rows('SELECT goal_steps.step_id AS step_id, goal_steps.goal_id AS goal_id, '
                'goal_steps.position AS position, goal_steps.scope_json AS scope_json, '
                'goal_steps.state AS state, goal_steps.attempts AS attempts, '
                'goal_steps.max_attempts AS max_attempts, goal_steps.lease_token AS lease_token, '
                'goal_steps.lease_until AS lease_until, goal_steps.captured_privacy_epoch AS '
                'captured_privacy_epoch, goal_steps.outcome_event_id AS outcome_event_id, '
                'goal_steps.last_error_code AS last_error_code, goal_steps.created_at AS '
                'created_at, goal_steps.updated_at AS updated_at FROM goal_steps AS goal_steps '
                "WHERE goal_steps.goal_id = %s AND goal_steps.state = 'WAITING_DECISION' ORDER BY "
                'goal_steps.position NULLS FIRST', (goal_id,))
            if (set(decision.step_order) != {step["step_id"] for step in pending}
                or any(step["attempts"] or dispatched(engine, step["step_id"])
                       for step in pending)):
                raise CoreError("GOAL_PLAN_ORDER_INVALID", 409)
            if decision.step_order != [step["step_id"] for step in pending]:
                positions = [step["position"] for step in pending]
                s.execute('UPDATE goal_steps SET position = position + 3 WHERE goal_id = %s AND '
                    "state = 'WAITING_DECISION'", (goal_id,))
                for position, step_id in zip(positions, decision.step_order, strict=True):
                    s.execute('UPDATE goal_steps SET position = %s, updated_at = %s WHERE step_id '
                        '= %s',
                               (position, now, step_id))
            first = decision.step_order[0]
            for source in evidence:
                s.execute('INSERT INTO goal_step_evidence VALUES (%s, %s) ON CONFLICT DO '
                    'NOTHING', (first, source))
            state = "QUEUED" if engine.settings.exploration_enabled else "WAITING_RESOURCE"
            s.execute('UPDATE goal_steps SET state = %s, updated_at = %s WHERE step_id = %s',
                      (state, now, first))
            s.execute('UPDATE goals SET state = %s, next_due_at = %s, version = version + 1, '
                'updated_at = %s WHERE goal_id = %s AND version = '
                '%s', (state, now, now, goal_id, goal["version"]))
            final = s.rows('SELECT step_id, position, state FROM goal_steps WHERE goal_id = %s '
                'ORDER BY position NULLS FIRST', (goal_id,))
            if (not 1 <= len(final) <= 3
                or [step["position"] for step in final] != list(range(1, len(final) + 1))
                or {step["step_id"] for step in final} != original_ids
                or {step["step_id"] for step in final if step["state"] != "DONE"}
                != set(decision.step_order)):
                raise CoreError("GOAL_PLAN_ORDER_INVALID", 409)
        s.audit("goal.continued", goal_id, goal_id, now, decision=decision.action,
                expected_version=decision.expected_version)
    return plan(engine, goal_id)
