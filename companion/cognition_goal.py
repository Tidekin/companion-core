"""Read bounded original step custody without current execution permission."""

from companion.memory_projections import ACTIVE


def record(reader, goal, sources):
    from companion.cognition_read import STATES, object_value, symbol

    rows = reader.store.rows(
        "SELECT goal_steps.step_id AS step_id, goal_steps.goal_id AS goal_id, "
        "goal_steps.position AS position, goal_steps.scope_json AS scope_json, goal_steps.state AS "
        "state, goal_steps.attempts AS attempts, goal_steps.max_attempts AS max_attempts, "
        "goal_steps.lease_token AS lease_token, goal_steps.lease_until AS lease_until, "
        "goal_steps.captured_privacy_epoch AS captured_privacy_epoch, goal_steps.outcome_event_id "
        "AS outcome_event_id, goal_steps.last_error_code AS last_error_code, goal_steps.created_at "
        "AS created_at, goal_steps.updated_at AS updated_at FROM goal_steps AS goal_steps WHERE "
        "goal_steps.goal_id = %s ORDER BY goal_steps.position NULLS FIRST, goal_steps.step_id "
        "NULLS FIRST LIMIT 4",
        (goal["goal_id"],),
    )
    if len(rows) > 3:
        return None
    valid_positions = [step["position"] for step in rows] == list(range(1, len(rows) + 1))
    steps, receipts, truncated, unknown, completed = [], [], False, False, 0
    for step in rows:
        roots = [
            r["event_id"]
            for r in reader.store.rows(
                (
                    'SELECT event_id FROM goal_step_evidence WHERE step_id '
                    '= %s ORDER BY event_id NULLS FIRST LIMIT 257'
                ),
                (step["step_id"],),
            )
        ]
        if not 1 <= len(roots) <= 256 or any(
            not reader.projections._goal_source(key, sources) for key in roots
        ):
            return None
        executions, cut = reader.projections._executions(goal, step, sources, None)
        available = reader.store.one(
            (
                (
                    'SELECT COUNT(*) n FROM (SELECT 1 FROM '
                    'exploration_requests WHERE redacted=0 AND '
                    "operation='read' AND (CASE WHEN goal_step_json IS JSON "
                    "THEN (goal_step_json::json#>>'{goal_id}') END)=%s AND "
                    '(CASE WHEN goal_step_json IS JSON THEN '
                    "(goal_step_json::json#>>'{step_id}') END)=%s LIMIT 21)"
                )
            ),
            (goal["goal_id"], step["step_id"]),
        )["n"]
        cut |= len(executions) < min(available, 20)
        uncertain = step["state"] == "EXECUTION_UNCERTAIN" or bool(
            reader.store.one(
                (
                    (
                        'SELECT 1 FROM exploration_requests r LEFT JOIN '
                        'reading_attempts a USING(request_id) WHERE (CASE WHEN '
                        'r.goal_step_json IS JSON THEN '
                        "(r.goal_step_json::json#>>'{goal_id}') END)=%s AND "
                        '(CASE WHEN r.goal_step_json IS JSON THEN '
                        "(r.goal_step_json::json#>>'{step_id}') END)=%s AND "
                        "(r.state='UNKNOWN' OR a.state='UNKNOWN') LIMIT 1"
                    )
                ),
                (goal["goal_id"], step["step_id"]),
            )
        )
        done = valid_positions and reader.projections._completed(goal, step, roots, sources, None)
        state = (
            "UNKNOWN"
            if not valid_positions or uncertain or step["state"] == "DONE" and not done
            else "done"
            if done
            else "failed"
            if step["state"] == "FAILED"
            else "closed"
            if step["state"] == "CANCELLED"
            else "running"
            if step["state"] == "RUNNING"
            else "planned"
        )
        steps.append(
            {
                **{key: step[key] for key in ("step_id", "position", "attempts", "max_attempts")},
                "state": symbol(step["state"], STATES),
                "operation": symbol(
                    object_value(step["scope_json"]).get("operation"),
                    {"read_content", "investigate"},
                ),
                "source_event_ids": roots,
                "outcome_event_id": step["outcome_event_id"] if done else None,
                "execution": {"state": state, "receipts": executions, "truncated": cut},
            }
        )
        receipts.extend(executions)
        truncated |= cut
        unknown |= state == "UNKNOWN"
        completed += int(done and not uncertain)
    state = "discussed"
    if goal["state"] in ACTIVE:
        state = "running" if goal["state"] == "RUNNING" else "planned"
    elif goal["state"] in {"FAILED", "CANCELLED"}:
        state = "failed" if goal["state"] == "FAILED" else "closed"
    elif goal["state"] == "DONE":
        state = (
            "done"
            if (
                rows
                and completed == len(rows)
                and goal["result_event_id"] == rows[-1]["outcome_event_id"]
            )
            else "UNKNOWN"
        )
    if unknown:
        state = "UNKNOWN"
    return {
        "steps": steps,
        "verified_completed_steps": completed,
        "execution": {"state": state, "receipts": receipts, "truncated": truncated},
    }
