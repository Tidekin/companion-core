"""Rebuildable attention and episode views; native evidence remains authoritative."""

from datetime import datetime

from companion.contracts import CoreError
from companion.memory_context import MemorySources, check_version, read_version
from companion.store import after, encode

ACTIVE = {"ACCEPTED", "QUEUED", "RUNNING", "WAITING_INPUT", "WAITING_RESOURCE", "RETRY_WAIT"}

# A nomination/order hint only; _goals still validates sources and real outcomes.
# Reused by episode nomination and the bounded goal projection so closed history
# cannot hide the unresolved goal that made an older episode eligible.
GOAL_ATTENTION = (

        'g.state IN (SELECT value FROM '
        'json_array_elements_text(%s::json) AS ids(value)) OR '
        "s.state='EXECUTION_UNCERTAIN' OR (g.kind='commitment' "
        "AND g.state!='CANCELLED' AND (c.state IS NULL OR "
        "c.state NOT IN ('FULFILLED','EXPLAINED_FAILURE') OR "
        'NOT EXISTS (SELECT 1 FROM outbox o WHERE '
        "o.message_id=c.feedback_message_id AND o.state='SENT' "
        "AND o.action_type='qq.private.reply'))) OR "
        "(g.state='DONE' AND (NOT EXISTS (SELECT 1 FROM "
        'goal_steps done WHERE done.goal_id=g.goal_id AND '
        "done.state='DONE' AND "
        'done.outcome_event_id=g.result_event_id AND '
        'done.position=(SELECT MAX(last.position) FROM '
        'goal_steps last WHERE last.goal_id=g.goal_id)) OR '
        '(SELECT COUNT(*) FROM goal_steps counted WHERE '
        'counted.goal_id=g.goal_id)<>(SELECT MAX(last.position) '
        'FROM goal_steps last WHERE last.goal_id=g.goal_id) OR '
        'EXISTS (SELECT 1 FROM goal_steps unfinished LEFT JOIN '
        'observations o ON o.event_id=unfinished.outcome_event_i'
        'd WHERE unfinished.goal_id=g.goal_id AND '
        '(unfinished.position NOT BETWEEN 1 AND 3 OR '
        "unfinished.state<>'DONE' OR o.event_id IS NULL OR "
        "o.redacted<>0 OR o.result_status NOT IN ('FETCHED',"
        "'WATCHED'))))) OR EXISTS (SELECT 1 FROM "
        'exploration_requests r LEFT JOIN reading_attempts a '
        'USING(request_id) WHERE r.redacted=0 AND '
        "r.operation='read' AND (CASE WHEN r.goal_step_json IS "
        "JSON THEN (r.goal_step_json::json#>>'{goal_id}') "
        'END)=g.goal_id AND (CASE WHEN r.goal_step_json IS JSON '
        "THEN (r.goal_step_json::json#>>'{step_id}') "
        "END)=s.step_id AND (r.state='UNKNOWN' OR "
        "a.state='UNKNOWN'))"

)


def identifiers(values):
    if (
        not isinstance(values, (list, tuple, set, frozenset))
        or len(values) > 100
        or any(not isinstance(value, str) or not value for value in values)
    ):
        raise CoreError("INVALID_PROJECTION_SCOPE", 400)
    return sorted(set(values))


class MemoryProjections:
    """Explicit bounded selection, never an authorization grant or recall filter.

    Callers supply IDs selected under their existing context authorization. An
    optional event allowlist must cover every source of an episode or goal.
    Temperature is independent of truth status, importance and retrieval score.
    """

    def __init__(self, memory, *, work=None):
        self.memory, self.store, self.work = memory, memory.store, work

    def continuation_candidates(self, conversation_id, now, selected_event_ids):
        """Bound each candidate lane before provenance and native heat validation.

        Separate lanes prevent recent episodes from hiding old active goals. SQL
        only nominates candidates: _goals and _temperature decide usable attention.
        """
        base = (
            "SELECT DISTINCT e.* FROM episodes e WHERE "
            "e.conversation_id=%s AND e.privacy_scope='private' AND "
            "e.status='ACTIVE' AND e.started_at<=%s "
        )
        goal = (
            "EXISTS (SELECT 1 FROM episode_events ee JOIN goal_evidence ge USING(event_id) "
            "JOIN goals g USING(goal_id) LEFT JOIN goal_steps s USING(goal_id) "
            "LEFT JOIN commitments c USING(goal_id) "
            "WHERE ee.episode_id=e.episode_id AND g.redacted=0 AND (" + GOAL_ATTENTION + "))"
        )
        lanes = [
            (
                (
                    (
                        'EXISTS (SELECT 1 FROM episode_events ee WHERE '
                        'ee.episode_id=e.episode_id AND ee.event_id IN (SELECT '
                        'value FROM json_array_elements_text(%s::json) AS '
                        'ids(value)))'
                    )
                ),
                encode(sorted(selected_event_ids)),
            ),
            (goal, encode(sorted(ACTIVE))),
            ("e.ended_at>=%s", after(now, -7 * 86400)),
        ]
        selected, truncated = {}, False
        for predicate, parameter in lanes:
            sql = base + "AND " + predicate + " ORDER BY e.ended_at DESC,e.episode_id LIMIT 21"
            args = (conversation_id, now, parameter)
            rows = (
                self.work.rows(self.store, sql, args, limit=21)
                if self.work
                else self.store.rows(sql, args)
            )
            truncated |= len(rows) > 20
            for row in rows[:20]:
                row.pop("record_sequence", None)
                if row["episode_id"] in selected:
                    continue
                if self.work:
                    if not self.work.take("candidates"):
                        break
                    if (
                        self.store.one(
                            "SELECT episodes.episode_id AS episode_id, episodes.title AS "
                            "title, episodes.summary AS summary, episodes.conversation_id AS "
                            "conversation_id, episodes.privacy_scope AS privacy_scope, "
                            "episodes.started_at AS started_at, episodes.ended_at AS ended_at, "
                            "episodes.status AS status, episodes.extractor_version AS "
                            "extractor_version, episodes.created_at AS created_at FROM episodes AS "
                            "episodes WHERE episodes.episode_id = %s",
                            (row["episode_id"],),
                        )
                        != row
                    ):
                        continue
                selected[row["episode_id"]] = row
        return sorted(
            selected.values(),
            key=lambda row: (
                row["ended_at"],
                row["episode_id"],
            ),
            reverse=True,
        ), truncated

    def snapshot(
        self,
        now,
        *,
        memory_ids=(),
        episode_ids=(),
        current_event_ids=(),
        used_memory_ids=(),
        event_ids=None,
        actor="owner",
    ):
        self.memory.require_owner(actor)
        mids, eids = identifiers(memory_ids), identifiers(episode_ids)
        current, used = set(identifiers(current_event_ids)), set(identifiers(used_memory_ids))
        allowed = None if event_ids is None else set(identifiers(event_ids))
        if not used <= set(mids) or allowed is not None and not current <= allowed:
            raise CoreError("INVALID_PROJECTION_SCOPE", 400)
        version = read_version(self.memory)
        sources = MemorySources(self.memory, now)
        memories, episodes = [], []
        for row in self.store.rows(
            "SELECT memories.memory_id AS memory_id, memories.subject AS subject, memories.kind AS "
            "kind, memories.content AS content, memories.status AS status, memories.origin AS "
            "origin, memories.confidence AS confidence, memories.importance AS importance, "
            "memories.privacy_scope AS privacy_scope, memories.supersedes_memory_id AS "
            "supersedes_memory_id, memories.created_at AS created_at, memories.updated_at AS "
            "updated_at, memories.valid_from AS valid_from, memories.valid_to AS valid_to, "
            "memories.last_reviewed_at AS last_reviewed_at, memories.fact_key AS fact_key, "
            "memories.needs_review AS needs_review FROM memories AS memories WHERE "
            'memories.memory_id IN (SELECT parsed.value COLLATE "C" AS _col_0 FROM '
            "JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS parsed(value)) AND "
            "memories.privacy_scope = 'private' AND memories.confidence > 0 AND memories.status IN "
            "('CONFIRMED', 'HYPOTHESIS') ORDER BY memories.memory_id NULLS FIRST",
            (encode(mids),),
        ):
            evidence = sources.evidence(row["memory_id"])
            if allowed is not None:
                evidence = [event for event in evidence if event["event_id"] in allowed]
            if not evidence or not sources.current(row):
                continue
            ids = [event["event_id"] for event in evidence]
            goals, truncated = self._goals(ids, sources, allowed)
            temperature, reasons = self._temperature(
                evidence,
                goals,
                current,
                now,
                row["memory_id"] in used,
            )
            memories.append(
                {
                    "memory_id": row["memory_id"],
                    "status": row["status"],
                    "temperature": temperature,
                    "attention_reasons": reasons,
                    "source_event_ids": ids,
                    "goals": goals,
                    "goals_truncated": truncated,
                }
            )
        for row in self.store.rows(
            "SELECT episodes.episode_id AS episode_id, episodes.title AS title, episodes.summary "
            "AS summary, episodes.conversation_id AS conversation_id, episodes.privacy_scope AS "
            "privacy_scope, episodes.started_at AS started_at, episodes.ended_at AS ended_at, "
            "episodes.status AS status, episodes.extractor_version AS extractor_version, "
            "episodes.created_at AS created_at FROM episodes AS episodes WHERE episodes.episode_id "
            'IN (SELECT parsed.value COLLATE "C" AS _col_0 FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s '
            "AS JSON)) AS parsed(value)) AND episodes.status = 'ACTIVE' AND episodes.privacy_scope "
            "= 'private' ORDER BY episodes.episode_id NULLS FIRST",
            (encode(eids),),
        ):
            ids = [
                r["event_id"]
                for r in self.store.rows(
                    (
                        'SELECT event_id FROM episode_events WHERE episode_id = '
                        '%s ORDER BY event_id NULLS FIRST LIMIT 21'
                    ),
                    (row["episode_id"],),
                )
            ]
            if not 1 <= len(ids) <= 20 or allowed is not None and not set(ids) <= allowed:
                continue
            evidence = [sources.event(key, require_text=False) for key in ids]
            if any(
                event is None or event["conversation_id"] != row["conversation_id"]
                for event in evidence
            ):
                continue
            goals, truncated = self._goals(ids, sources, allowed)
            temperature, reasons = self._temperature(evidence, goals, current, now)
            deliveries = self.store.rows(
                "SELECT message_id, state, sent_at FROM outbox WHERE origin_event_id IN (SELECT "
                'value COLLATE "C" FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS '
                "parsed(value)) AND conversation_id = %s AND action_type = 'qq.private.reply' "
                "ORDER BY created_at NULLS FIRST, message_id NULLS FIRST LIMIT "
                "21",
                (encode(ids), row["conversation_id"]),
            )
            episodes.append(
                {
                    "episode_id": row["episode_id"],
                    "state": "discussed",
                    "temperature": temperature,
                    "attention_reasons": reasons,
                    "source_event_ids": ids,
                    "goals": goals,
                    "goals_truncated": truncated,
                    "deliveries": [
                        {
                            **delivery,
                            "state": delivery["state"],
                            "told": delivery["state"] == "SENT",
                        }
                        for delivery in deliveries[:20]
                    ],
                    "deliveries_truncated": len(deliveries) > 20,
                }
            )
        check_version(self.memory, version)
        return {
            "projection_version": 1,
            "as_of": now,
            "privacy_epoch": version[0],
            "memories": memories,
            "episodes": episodes,
        }

    @staticmethod
    def _temperature(events, goals, current, now, used=False):
        reasons = []
        if any(event["event_id"] in current for event in events):
            reasons.append("CURRENT_CONTEXT")
        if used:
            reasons.append("USED_IN_CONTEXT")
        if any(
            goal["native_state"] in ACTIVE
            or goal["state"] == "UNKNOWN"
            or goal["kind"] == "commitment"
            and not goal["closed"]
            for goal in goals
        ):
            reasons.append("UNRESOLVED_GOAL")
        if reasons:
            return "Hot", reasons
        if any(
            datetime.fromisoformat(event["occurred_at"])
            >= datetime.fromisoformat(after(now, -7 * 86400))
            for event in events
        ):
            return "Warm", ["RECENT_EVIDENCE"]
        return "Cold", ["OLDER_EVIDENCE"]

    def _goals(self, ids, sources, allowed):
        def read(sql, args):
            return (
                self.work.rows(self.store, sql, args, limit=21)
                if self.work
                else self.store.rows(sql, args)
            )

        rows = read(
            (
                (
                    'SELECT g.* FROM goals g JOIN goal_evidence ge '
                    'USING(goal_id) LEFT JOIN goal_steps s USING(goal_id) '
                    'LEFT JOIN commitments c USING(goal_id) WHERE '
                    'g.redacted=0 AND ge.event_id IN (SELECT value FROM '
                    'json_array_elements_text(%s::json) AS ids(value)) '
                    'GROUP BY g.goal_id ORDER BY bool_or('
                )
            )
            + GOAL_ATTENTION
            + ") DESC NULLS LAST,g.goal_id LIMIT 21",
            (encode(ids), encode(sorted(ACTIVE))),
        )
        for row in rows:
            row.pop("record_sequence", None)
        result = []
        for goal in rows[:20]:
            if (
                self.work
                and self.store.one(
                    (
                        'SELECT goals.goal_id AS goal_id, goals.kind AS kind, '
                        'goals.topic AS topic, goals.scope_json AS scope_json, '
                        'goals.proposal_key AS proposal_key, goals.state AS '
                        'state, goals.priority AS priority, goals.next_due_at '
                        'AS next_due_at, goals.last_error_code AS '
                        'last_error_code, goals.version AS version, '
                        'goals.result_event_id AS result_event_id, '
                        'goals.created_at AS created_at, goals.updated_at AS '
                        'updated_at, goals.redacted AS redacted, '
                        'goals.reflection_suggestion_id AS '
                        'reflection_suggestion_id, goals.authority_json AS '
                        'authority_json FROM goals AS goals WHERE goals.goal_id '
                        '= %s'
                    ),
                    (goal["goal_id"],),
                )
                != goal
            ):
                continue
            roots = [
                r["event_id"]
                for r in self.store.rows(
                    "SELECT event_id FROM goal_evidence WHERE goal_id = %s ORDER BY event_id NULLS "
                    "FIRST LIMIT 101",
                    (goal["goal_id"],),
                )
            ]
            if (
                not 1 <= len(roots) <= 100
                or allowed is not None
                and not set(roots) <= allowed
                or any(not self._goal_source(key, sources) for key in roots)
            ):
                continue
            steps = self.store.rows(
                "SELECT goal_steps.step_id AS step_id, goal_steps.goal_id AS "
                "goal_id, goal_steps.position AS position, goal_steps.scope_json AS scope_json, "
                "goal_steps.state AS state, goal_steps.attempts AS attempts, "
                "goal_steps.max_attempts AS max_attempts, goal_steps.lease_token AS lease_token, "
                "goal_steps.lease_until AS lease_until, goal_steps.captured_privacy_epoch AS "
                "captured_privacy_epoch, goal_steps.outcome_event_id AS outcome_event_id, "
                "goal_steps.last_error_code AS last_error_code, goal_steps.created_at AS "
                "created_at, goal_steps.updated_at AS updated_at FROM goal_steps AS goal_steps "
                "WHERE goal_steps.goal_id = %s ORDER BY goal_steps.position NULLS FIRST LIMIT "
                "4",
                (goal["goal_id"],),
            )
            valid_positions = 1 <= len(steps) <= 3 and [step["position"] for step in steps] == list(
                range(1, len(steps) + 1)
            )
            state = "discussed"
            if goal["state"] in ACTIVE:
                state = "running" if goal["state"] == "RUNNING" else "planned"
            elif goal["state"] == "FAILED":
                state = "failed"
            elif goal["state"] == "CANCELLED":
                state = "closed"
            elif goal["state"] == "DONE":
                completed = (
                    valid_positions
                    and goal["result_event_id"] == steps[-1]["outcome_event_id"]
                    and all(self._completed(goal, step, roots, sources, allowed) for step in steps)
                )
                state = "done" if completed else "UNKNOWN"
            if steps and not valid_positions:
                state = "UNKNOWN"
            if any(step["state"] == "EXECUTION_UNCERTAIN" for step in steps):
                state = "UNKNOWN"
            executions, executions_truncated = [], len(steps) > 3
            for step in steps[:3]:
                found, truncated = self._executions(goal, step, sources, allowed)
                executions.extend(found)
                executions_truncated |= truncated
            executions.sort(
                key=lambda item: (
                    item["state"] != "UNKNOWN" and item["attempt_state"] != "UNKNOWN",
                    item["request_id"],
                )
            )
            executions_truncated |= len(executions) > 20
            executions = executions[:20]
            if any(
                item["state"] == "UNKNOWN" or item["attempt_state"] == "UNKNOWN"
                for item in executions
            ):
                state = "UNKNOWN"
            commitment = self.store.one(
                "SELECT c.state, c.feedback_outcome, o.state AS delivery_state FROM commitments AS "
                "c LEFT JOIN outbox AS o ON o.message_id = c.feedback_message_id AND o.action_type "
                "= 'qq.private.reply' WHERE c.goal_id = %s",
                (goal["goal_id"],),
            )
            told = bool(commitment and commitment["delivery_state"] == "SENT")
            closed = state == "closed" or bool(
                told
                and commitment["state"] in {"FULFILLED", "EXPLAINED_FAILURE"}
                and state in {"done", "failed"}
            )
            result.append(
                {
                    "goal_id": goal["goal_id"],
                    "kind": goal["kind"],
                    "native_state": goal["state"],
                    "state": state,
                    "steps": [
                        {key: step[key] for key in ("step_id", "position", "state")}
                        for step in steps[:3]
                    ],
                    "executions": executions,
                    "executions_truncated": executions_truncated,
                    "told": told,
                    "closed": closed,
                    "source_event_ids": roots,
                }
            )
        return result, len(rows) > 20

    def _goal_source(self, key, sources):
        # Facts remain owner-only. Native goals may also cite actual observations.
        if sources.event(key, require_text=False) is not None:
            return True
        observation = self.store.one(
            "SELECT 1 FROM observations AS o JOIN events AS e USING (event_id) WHERE o.event_id = "
            "%s AND o.redacted = 0 AND e.event_type = 'content.observed'",
            (key,),
        )
        return bool(observation and self._live_lineage(list(sources._lineage(key)), sources))

    def _executions(self, goal, step, sources, allowed):
        if not step:
            return [], False

        def read(sql, args):
            return (
                self.work.rows(self.store, sql, args, limit=21)
                if self.work
                else self.store.rows(sql, args)
            )

        rows = read(
            (
                (
                    'SELECT r.request_id,r.state,a.state AS attempt_state '
                    'FROM exploration_requests r LEFT JOIN reading_attempts '
                    'a USING(request_id) WHERE r.redacted=0 AND '
                    "r.operation='read' AND (CASE WHEN r.goal_step_json IS "
                    "JSON THEN (r.goal_step_json::json#>>'{goal_id}') "
                    'END)=%s AND (CASE WHEN r.goal_step_json IS JSON THEN '
                    "(r.goal_step_json::json#>>'{step_id}') END)=%s ORDER "
                    "BY (r.state='UNKNOWN' OR COALESCE(a.state='UNKNOWN',"
                    'false)) DESC,r.created_at DESC,r.request_id LIMIT 21'
                )
            ),
            (goal["goal_id"], step["step_id"]),
        )
        executions = []
        for row in rows[:20]:
            if (
                self.work
                and self.store.one(
                    (
                        (
                            'SELECT r.request_id,r.state,a.state AS attempt_state '
                            'FROM exploration_requests r LEFT JOIN reading_attempts '
                            'a USING(request_id) WHERE r.request_id=%s AND '
                            "r.redacted=0 AND r.operation='read' AND (CASE WHEN "
                            'r.goal_step_json IS JSON THEN '
                            "(r.goal_step_json::json#>>'{goal_id}') END)=%s AND "
                            '(CASE WHEN r.goal_step_json IS JSON THEN '
                            "(r.goal_step_json::json#>>'{step_id}') END)=%s"
                        )
                    ),
                    (row["request_id"], goal["goal_id"], step["step_id"]),
                )
                != row
            ):
                continue
            ids = [
                r["event_id"]
                for r in self.store.rows(
                    (
                        'SELECT event_id FROM exploration_evidence WHERE '
                        'request_id = %s ORDER BY event_id NULLS FIRST LIMIT 101'
                    ),
                    (row["request_id"],),
                )
            ]
            if (
                not 1 <= len(ids) <= 100
                or allowed is not None
                and not set(ids) <= allowed
                or any(not self._goal_source(key, sources) for key in ids)
            ):
                continue
            executions.append({**row, "step_id": step["step_id"]})
        return executions, len(rows) > 20

    def _completed(self, goal, step, roots, sources, allowed):
        key = step["outcome_event_id"] if step else None
        if not key or step["state"] != "DONE":
            return False
        if allowed is not None and key not in allowed:
            return False
        receipt = self.store.one(
            "SELECT result_status FROM observations WHERE event_id = %s AND redacted = 0",
            (key,),
        )
        if not receipt or receipt["result_status"] not in {"FETCHED", "WATCHED"}:
            return False
        lineage = list(sources._lineage(key))
        evidence = self.store.rows(
            "SELECT event_id FROM goal_step_evidence WHERE step_id=%s"
            + (" LIMIT 101" if self.work else ""),
            (step["step_id"],),
        )
        if self.work and len(evidence) > 100:
            from companion.retrieval_work import WorkLimit

            self.work.mark("source_nodes")
            raise WorkLimit("source_nodes")
        required = set(roots) | {row["event_id"] for row in evidence}
        if not required <= {event["source_id"] for event in lineage}:
            return False
        return self._live_lineage(lineage, sources)

    @staticmethod
    def _live_lineage(lineage, sources):
        now = datetime.fromisoformat(sources.now)
        if not lineage:
            return False
        for event in lineage:
            if (
                event["event_id"] is None
                or event["redacted"]
                or event["erased"]
                or event["privacy_scope"] != "private"
                or event["scope"] != "private"
                or event["owner_actor_id"] != "owner"
                or event["source"] == "observation.simulation"
                or datetime.fromisoformat(event["ingested_at"]) < sources.cutoff
                or datetime.fromisoformat(event["occurred_at"]) > now
                or datetime.fromisoformat(event["ingested_at"]) > now
            ):
                return False
        return True
