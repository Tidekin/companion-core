"""Bounded N1/N2/N3 steps inside the original natural-day coordinator and leases."""

from contextlib import nullcontext
from datetime import date, datetime

from companion import budget, owner_authority
from companion.contracts import CoreError
from companion.diary import day_bounds
from companion.goals import GoalProposal
from companion.memory import Memory
from companion.memory_authority import source_allowed
from companion.memory_work import TERMINAL_PHASES, checkpoint_state, extraction_sources
from companion.store import after
from companion.worker_schedule import UnitResult

TERMINAL = {"COMPLETE", "COMMITTED", "SKIPPED", "FAILED", "CANCELLED", "UNKNOWN"}


def _permitted(nightly, purpose):
    if not owner_authority.required(nightly.store, nightly.settings):
        return True
    view = owner_authority.current(nightly.store, nightly.settings, nightly.clock,
                                   purpose="consolidation")
    return purpose in view.policy.purposes


def _yield(nightly, run, *, waiting=False, code=None, due=None):
    now = nightly.clock()
    due = due or after(now, 60) if waiting else now
    nightly.checkpoint(run, "PARTIAL", code, waiting=True, next_attempt_at=due)
    return UnitResult(not waiting, datetime.fromisoformat(due))


def _next(nightly):
    row = nightly.store.one(
        "SELECT MIN(CASE WHEN state = 'LEASED' THEN lease_until ELSE next_attempt_at END) AS due "
        "FROM consolidation_runs WHERE timezone = %s AND state IN ('LEASED', 'PARTIAL')",
        (nightly.settings.timezone,),
    )
    return datetime.fromisoformat(row["due"]) if row and row["due"] else None


def _episodes(nightly, run, memory):
    steps = run["steps"]
    if not nightly.settings.memory_enabled or not _permitted(nightly, "memory"):
        steps["n2"] = "SKIPPED"
        return _yield(nightly, run)
    start, end = day_bounds(date.fromisoformat(run["local_date"]), run["timezone"])
    # Source selection is N1's existing Diary source contract. Contextual older
    # episodes and real observations retain their identities; neither becomes QQ.
    ids = sorted({source["event_id"] for source in run["sources"]
                  if source["kind"] == "owner_message"
                  and start <= source["occurred_at"] < end})
    done = set(steps.get("episode_sources", []))
    pending = [key for key in ids if key not in done]
    if not pending:
        steps["n2"] = "MATERIAL_READY"
        return _yield(nightly, run)
    first = nightly.store.one('SELECT events.event_id AS event_id, events.schema_version AS '
        'schema_version, events.source AS source, events.source_event_id AS source_event_id, '
        'events.conversation_id AS conversation_id, events.actor_id AS actor_id, events.event_type '
        'AS event_type, events.occurred_at AS occurred_at, events.ingested_at AS ingested_at, '
        'events.privacy_scope AS privacy_scope, events.body_json AS body_json, events.trace_id AS '
        'trace_id, events.idempotency_key AS idempotency_key, events.fingerprint AS fingerprint, '
        'events.redacted AS redacted, events.memory_processed AS memory_processed, '
        'events.reply_revision AS reply_revision FROM events AS events WHERE events.event_id = '
        '%s', (pending[0],))
    events = []
    for key in pending:
        event = nightly.store.one('SELECT events.event_id AS event_id, events.schema_version AS '
            'schema_version, events.source AS source, events.source_event_id AS source_event_id, '
            'events.conversation_id AS conversation_id, events.actor_id AS actor_id, '
            'events.event_type AS event_type, events.occurred_at AS occurred_at, '
            'events.ingested_at AS ingested_at, events.privacy_scope AS privacy_scope, '
            'events.body_json AS body_json, events.trace_id AS trace_id, events.idempotency_key AS '
            'idempotency_key, events.fingerprint AS fingerprint, events.redacted AS redacted, '
            'events.memory_processed AS memory_processed, events.reply_revision AS reply_revision '
            'FROM events AS events WHERE events.event_id = %s', (key,))
        if event and first and event["conversation_id"] == first["conversation_id"]:
            if not source_allowed(nightly.store, nightly.settings, nightly.clock, key, "memory"):
                raise CoreError("PRIVACY_CHANGED", 409)
            events.append(event)
        if len(events) == 20:
            break
    if not events:
        raise CoreError("PRIVACY_CHANGED", 409)
    with nightly.store.transaction():
        episode_id = memory.episodes(events, nightly.clock(), mark_processed=False)
        steps["episode_sources"] = sorted(done | {event["event_id"] for event in events})
        covered = memory.episode_coverage([event["event_id"] for event in events], nightly.clock())
        steps["warm_episode_ids"] = sorted(set(steps.get("warm_episode_ids", []))
            | {item["episode_id"] for item in covered} | ({episode_id} if episode_id else set()))
        steps["n2"] = "MATERIAL_READY" if set(ids) <= set(steps["episode_sources"]) else "RUNNING"
        return _yield(nightly, run)


async def _memory(nightly, run, day):
    steps = run["steps"]
    if not nightly.settings.memory_enabled or not _permitted(nightly, "memory"):
        steps["memory"] = "SKIPPED"
        return _yield(nightly, run)
    def persist(record):
        store = nightly.store
        with nullcontext() if store.in_transaction else store.transaction():
            # Memory batches can extend beyond Diary's bounded display sources.
            # Pending candidates must still be owned by the existing erasure closure.
            batch = record["batch"]
            if batch.get("phase") in {"REVIEW", "REVIEWING", "COMPLETE"}:
                steps["n2"] = "COMPLETE"
                steps["n3"] = "RUNNING"
            elif batch.get("phase") in {"SKIPPED", "FAILED", "UNKNOWN", "CANCELLED"}:
                steps["n2"] = batch["phase"]
            ids = record["source_event_ids"]
            for key in ids:
                store.execute('INSERT INTO consolidation_evidence VALUES (%s, %s) ON CONFLICT DO '
                    'NOTHING',
                              (run["run_id"], key))
            if batch.get("committed"):
                nightly.after_memory_commit(run)
            # The singleton native extraction checkpoint owns text/proofs. This
            # day stores only progress and original references, never another copy.
            steps["memory_batch"] = {key: value for key, value in batch.items()
                                     if key in {"phase", "request_id", "extraction_request_id",
                                                "error", "memory_ids", "episode_ids"}}
            steps["memory_batch"]["batch_id"] = record["batch_id"]
            nightly.checkpoint(run)

    result = await nightly.extract_step(day, on_checkpoint=persist)
    if not isinstance(result, UnitResult):
        raise CoreError("NIGHTLY_EXTRACTION_STEP_REQUIRED", 500)
    latest = checkpoint_state(nightly.store)
    if not result.progressed and latest and latest["batch"].get("phase") in TERMINAL_PHASES:
        if (latest["batch_id"] == steps.get("memory_batch", {}).get("batch_id")
            or latest["local_day"] == day.isoformat() and latest["timezone"] == run["timezone"]):
            persist(latest)
    batch = steps.get("memory_batch", {})
    phase = batch.get("phase")
    if phase == "COMPLETE":
        nightly.after_memory_commit(run)
        steps["warm_memory_ids"] = sorted(set(steps.get("warm_memory_ids", []))
                                           | set(batch.get("memory_ids", [])))
        # The source query is shared with native extraction; no second eligibility policy.
        # Only attributes used by that query are exposed through the coordinator.
        remaining = extraction_sources(nightly, day)
        steps["memory"] = ("WAITING" if len(remaining) >= nightly.settings.extraction_min_events
                           else "COMPLETE")
        if steps["memory"] == "WAITING":
            steps.pop("memory_batch", None)
        return _yield(nightly, run)
    if phase in {"SKIPPED", "FAILED", "UNKNOWN", "CANCELLED"}:
        steps["memory"] = phase
        return _yield(nightly, run)
    if not result.progressed and len(extraction_sources(nightly, day)) < (
        nightly.settings.extraction_min_events
    ) and (not latest or latest["batch"].get("phase") in TERMINAL_PHASES):
        steps["memory"] = "SKIPPED"
        if steps.get("n2") == "MATERIAL_READY":
            steps["n2"] = "SKIPPED"
        return _yield(nightly, run)
    steps["memory"] = "WAITING"
    return _yield(nightly, run, waiting=not result.progressed,
                  code=batch.get("error") or (None if result.progressed else "MEMORY_PENDING"),
                  due=result.next_deadline.isoformat() if result.next_deadline else None)


async def _diary(nightly, run, day, worker_id):
    steps = run["steps"]
    if not _permitted(nightly, "diary"):
        steps["diary"] = "SKIPPED"
        return _yield(nightly, run)
    diary_id = nightly.diary.schedule_day(day)
    if not diary_id:
        steps["diary"] = "SKIPPED"
        return _yield(nightly, run)
    row = nightly.store.one('SELECT diary_runs.run_id AS run_id, diary_runs.identity_id AS '
        'identity_id, diary_runs.local_date AS local_date, diary_runs.timezone AS timezone, '
        'diary_runs.state AS state, diary_runs.source_set_sha256 AS source_set_sha256, '
        'diary_runs.captured_privacy_epoch AS captured_privacy_epoch, diary_runs.lease_owner AS '
        'lease_owner, diary_runs.lease_token AS lease_token, diary_runs.lease_until AS '
        'lease_until, diary_runs.next_attempt_at AS next_attempt_at, diary_runs.attempts AS '
        'attempts, diary_runs.max_attempts AS max_attempts, diary_runs.model_request_id AS '
        'model_request_id, diary_runs.last_error_code AS last_error_code, diary_runs.created_at AS '
        'created_at, diary_runs.updated_at AS updated_at FROM diary_runs AS diary_runs WHERE '
        'diary_runs.run_id = %s', (diary_id,))
    if row["state"] in {"PENDING", "RETRY_WAIT"} or (
        row["state"] == "RUNNING" and row["lease_until"] <= nightly.clock()
    ):
        await nightly.diary.generate_once(worker_id, run_id=diary_id)
        row = nightly.store.one('SELECT diary_runs.run_id AS run_id, diary_runs.identity_id AS '
            'identity_id, diary_runs.local_date AS local_date, diary_runs.timezone AS timezone, '
            'diary_runs.state AS state, diary_runs.source_set_sha256 AS source_set_sha256, '
            'diary_runs.captured_privacy_epoch AS captured_privacy_epoch, diary_runs.lease_owner '
            'AS lease_owner, diary_runs.lease_token AS lease_token, diary_runs.lease_until AS '
            'lease_until, diary_runs.next_attempt_at AS next_attempt_at, diary_runs.attempts AS '
            'attempts, diary_runs.max_attempts AS max_attempts, diary_runs.model_request_id AS '
            'model_request_id, diary_runs.last_error_code AS last_error_code, '
            'diary_runs.created_at AS created_at, diary_runs.updated_at AS updated_at FROM '
            'diary_runs AS diary_runs WHERE diary_runs.run_id = %s', (diary_id,))
    steps["diary"] = row["state"]
    if row["state"] not in TERMINAL:
        due = row["lease_until"] if row["state"] == "RUNNING" else row["next_attempt_at"]
        return _yield(nightly, run, waiting=True, code="DIARY_PENDING", due=due)
    return _yield(nightly, run)


async def _reflection(nightly, run, day):
    result = await nightly.reflections.run_day(day) if (
        nightly.settings.reflection_enabled and _permitted(nightly, "reflection")) else None
    run["steps"]["reflection"] = result["state"] if result else "SKIPPED"
    if result and result["state"] in {"RUNNING", "RETRY_WAIT"}:
        return _yield(nightly, run, waiting=True, code="REFLECTION_PENDING",
                      due=result.get("next_attempt_at"))
    return _yield(nightly, run)


def _goals(nightly, run, day):
    if not nightly.settings.goals_enabled or not _permitted(nightly, "goals"):
        run["steps"]["goals"] = "SKIPPED"
        return _yield(nightly, run)
    proposals = nightly.store.rows(
        'SELECT s.suggestion_id, s.content, r.run_id FROM reflection_suggestions AS s JOIN '
        'reflection_runs AS r USING (run_id) WHERE r.local_date = %s AND r.timezone = %s AND '
        "r.state = 'COMMITTED' AND s.kind = 'next_question' AND s.state IN ('PENDING', "
        "'ACCEPT_AS_HYPOTHESIS')",
        (day.isoformat(), run["timezone"]),
    )
    for proposal in proposals:
        ids = [row["event_id"] for row in nightly.store.rows(
            'SELECT DISTINCT event_id FROM reflection_evidence WHERE run_id = %s',
            (proposal["run_id"],),
        )]
        try:
            nightly.goals.propose(
                GoalProposal(topic=proposal["content"][:300], source_event_ids=ids),
                reflection_suggestion_id=proposal["suggestion_id"],
            )
        except CoreError as error:
            if error.code != "GOAL_CAPACITY_REACHED":
                raise
            run["steps"]["goals"] = "WAITING"
            return _yield(nightly, run, waiting=True, code=error.code)
    run["steps"]["goals"] = "COMPLETE"
    return _yield(nightly, run)


async def run_step(nightly, worker_id):
    if budget.chat_pending(nightly.store, nightly.clock()):
        return UnitResult(False, _next(nightly))
    run = nightly.claim()
    if run is None:
        return UnitResult(False, _next(nightly))
    day, steps = date.fromisoformat(run["local_date"]), run["steps"]
    try:
        nightly.reflections.validate_day(day)
        if not run["sources"]:
            steps.update(n1="SKIPPED", n2="SKIPPED", n3="SKIPPED")
            nightly.checkpoint(run, "SKIPPED", "NO_SOURCES")
            return UnitResult(True)
        if steps.get("n1") != "COMPLETE":
            # claim already rechecks original owner rights, exact sources and epoch.
            # Actual budget reservation remains immediately before each model call.
            steps["n1"] = "COMPLETE"
            return _yield(nightly, run)
        if steps.get("n2") not in TERMINAL | {"MATERIAL_READY"}:
            return _episodes(nightly, run, Memory(nightly.store, nightly.settings,
                                                 nightly.diary.ledger))
        if steps.get("memory") not in TERMINAL:
            return await _memory(nightly, run, day)
        steps["n3"] = "RUNNING"
        if steps.get("diary") not in TERMINAL:
            return await _diary(nightly, run, day, worker_id)
        if steps.get("reflection") not in TERMINAL:
            return await _reflection(nightly, run, day)
        if steps.get("goals") not in {"COMPLETE", "SKIPPED"}:
            return _goals(nightly, run, day)
        failed = any(steps.get(key) in {"FAILED", "CANCELLED", "UNKNOWN"}
                     for key in ("memory", "diary", "reflection"))
        steps["n3"] = "PARTIAL" if failed else "COMPLETE"
        # Pending text/proofs are temporary model work, not an enduring memory copy.
        batch = steps.get("memory_batch")
        if batch:
            steps["memory_batch"] = {key: value for key, value in batch.items()
                                     if key in {"phase", "request_id", "extraction_request_id",
                                                "error", "memory_ids", "episode_ids"}}
        nightly.checkpoint(run, "PARTIAL" if failed else "COMPLETE",
                           "STEP_FAILED" if failed else None)
        return UnitResult(True)
    except Exception as error:
        code = error.code if isinstance(error, CoreError) else "CONSOLIDATION_INTERNAL_ERROR"
        nightly.store.execute(
            "UPDATE consolidation_runs SET state = %s, lease_token = NULL, steps_json = '{}', "
            "last_error_code = %s, updated_at = %s WHERE run_id = %s AND state = 'LEASED' AND "
            'lease_token = %s',
            ("CANCELLED" if code == "PRIVACY_CHANGED" else "FAILED", code, nightly.clock(),
             run["run_id"], run["lease_token"]),
        )
        return UnitResult(True)
