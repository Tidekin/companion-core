"""Private fiction on native jobs, evidence, accounting and erasure; never facts."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from companion import budget, owner_authority, paid_budget, reply_revision
from companion.contracts import CoreError
from companion.diary import day_bounds
from companion.memory import Memory
from companion.memory_context import MemoryContext, MemorySources, check_version, read_version
from companion.privacy import ErasureLedger, redact, sensitive
from companion.rem_protocol import RULES, RemDraft
from companion.store import after, encode, uid


def _authority(store, settings, clock, *, authority=None):
    if not settings.rem.enabled or not settings.memory_enabled or settings.mode != "live":
        raise CoreError("REM_DISABLED", 403)
    view = owner_authority.current(store, settings, clock, purpose="rem", authority=authority)
    owner_authority.current(store, settings, clock, purpose="memory", authority=view.authority)
    from companion.erasure import cleanup_contracts

    if not any(contract.name == "rem" for contract in cleanup_contracts()):
        raise CoreError("REM_ERASURE_CONTRACT_REQUIRED", 503)
    return view


def _context(store, settings):
    return MemoryContext(Memory(store, settings, ErasureLedger(settings.erasure_ledger)))


def _material(core, retention_days):
    """Nominate at most twenty facts and retain at most three unchanged native facts."""
    now, cfg = core.clock(), core.settings
    sources = MemorySources(core.memory, now)
    cutoff = datetime.fromisoformat(after(now, -86400 * retention_days))
    selected = []
    for row in core.store.rows(
        "SELECT memories.memory_id AS memory_id, memories.subject AS subject, memories.kind AS "
        "kind, memories.content AS content, memories.status AS status, memories.origin AS origin, "
        "memories.confidence AS confidence, memories.importance AS importance, "
        "memories.privacy_scope AS privacy_scope, memories.supersedes_memory_id AS "
        "supersedes_memory_id, memories.created_at AS created_at, memories.updated_at AS "
        "updated_at, memories.valid_from AS valid_from, memories.valid_to AS valid_to, "
        "memories.last_reviewed_at AS last_reviewed_at, memories.fact_key AS fact_key, "
        "memories.needs_review AS needs_review FROM memories AS memories WHERE "
        "memories.privacy_scope = 'private' AND memories.status = 'CONFIRMED' AND "
        "memories.confidence > 0 AND memories.needs_review = 0 ORDER BY memories.updated_at DESC "
        "NULLS LAST, memories.memory_id NULLS FIRST LIMIT 20",
    ):
        evidence = sources.evidence(row["memory_id"])
        if (
            not evidence
            or not sources.current(row)
            or sensitive(row["content"])
            or redact(row["content"], cfg) != row["content"]
            or any(
                datetime.fromisoformat(item["ingested_at"]) <= cutoff
                for root in evidence
                for item in sources._lineage(root["event_id"])
            )
        ):
            continue
        selected.append({**row, "evidence_ids": [item["event_id"] for item in evidence]})
        if len(selected) == cfg.rem.max_source_memories:
            break
    if not selected:
        return None
    proof = core.memory_context.pin(now, rows=selected)
    inputs = [
        {key: row[key] for key in ("memory_id", "subject", "content", "status")} for row in selected
    ]
    roots = [
        core.store.one(
            (
                'SELECT events.event_id AS event_id, '
                'events.schema_version AS schema_version, events.source '
                'AS source, events.source_event_id AS source_event_id, '
                'events.conversation_id AS conversation_id, '
                'events.actor_id AS actor_id, events.event_type AS '
                'event_type, events.occurred_at AS occurred_at, '
                'events.ingested_at AS ingested_at, '
                'events.privacy_scope AS privacy_scope, '
                'events.body_json AS body_json, events.trace_id AS '
                'trace_id, events.idempotency_key AS idempotency_key, '
                'events.fingerprint AS fingerprint, events.redacted AS '
                'redacted, events.memory_processed AS memory_processed, '
                'events.reply_revision AS reply_revision FROM events AS '
                'events WHERE events.event_id = %s'
            ),
            (key,),
        )
        for key in proof["source_event_ids"]
    ]
    if any(root is None for root in roots):
        raise CoreError("PRIVACY_CHANGED", 409)
    expires = min(
        [
            after(now, 86400 * cfg.rem.record_retention_days),
            *[after(row["ingested_at"], 86400 * retention_days) for row in roots],
        ]
    )
    return inputs, proof, roots[0]["conversation_id"], expires


def schedule_due(core):
    """Called by the existing selector; at most one creation per local day."""
    if not core.settings.rem.enabled:
        return None
    store, cfg, clock = core.store, core.settings, core.clock
    with store.transaction():
        try:
            view = _authority(store, cfg, clock)
        except CoreError:
            return None
        local = datetime.fromisoformat(clock()).astimezone(ZoneInfo(cfg.timezone))
        if local.hour < cfg.rem.local_hour or budget.chat_pending(store, clock()):
            return None
        day = local.date().isoformat()
        if store.one(
            "SELECT 1 FROM rem_records WHERE local_date = %s AND timezone = %s", (day, cfg.timezone)
        ):
            return None
        material = _material(
            core,
            min(
                cfg.rem.source_retention_days,
                view.policy.source_retention_days,
                cfg.raw_event_retention_days,
            ),
        )
        if material is None:
            return None
        inputs, proof, conversation, expires = material
        return _enqueue(
            store,
            cfg,
            clock,
            day,
            conversation,
            expires,
            {"inputs": inputs, "proof": proof},
            proof["source_event_ids"],
            memory_ids=[row["memory_id"] for row in inputs],
        )


def _enqueue(
    store,
    settings,
    clock,
    day,
    conversation,
    expires,
    snapshot,
    sources,
    *,
    memory_ids=(),
    idempotency_key=None,
):
    """One existing REM job/record and its evidence commit atomically for either producer."""
    if not store.in_transaction:
        raise RuntimeError("REM_ADMISSION_TRANSACTION_REQUIRED")
    rem_id, event_id, job_id, now = uid("rem"), uid("evt_rem"), uid("job"), clock()
    store.insert(
        "events",
        event_id=event_id,
        schema_version=1,
        source="system.control",
        source_event_id=rem_id,
        conversation_id=conversation,
        actor_id="system",
        event_type="rem.requested",
        occurred_at=now,
        ingested_at=now,
        privacy_scope="private",
        body_json=encode({"rem_id": rem_id}),
        trace_id=rem_id,
        memory_processed=1,
    )
    store.insert(
        "jobs",
        job_id=job_id,
        event_id=event_id,
        kind="rem",
        state="QUEUED",
        idempotency_key=idempotency_key or rem_id,
        max_attempts=1,
        created_at=now,
        updated_at=now,
        context_tracked=1,
    )
    store.insert("job_sources", job_id=job_id, event_id=event_id)
    owner_authority.bind_job(store, settings, clock, job_id, purpose="rem")
    for key in sorted(set(sources)):
        store.insert("event_dependencies", event_id=event_id, source_event_id=key)
        store.insert("job_sources", job_id=job_id, event_id=key)
    store.insert(
        "rem_records",
        rem_id=rem_id,
        job_id=job_id,
        local_date=day,
        timezone=settings.timezone,
        config_sha256=paid_budget.sha(settings.rem.model_dump()),
        source_memory_ids_json=encode(list(memory_ids)),
        source_event_ids_json=encode(sorted(set(sources))),
        source_snapshot_json=encode(snapshot),
        created_at=now,
        expires_at=expires,
    )
    store.audit("rem.scheduled", rem_id, rem_id, now, kind="FICTION")
    return job_id


def _life_draft(store, settings, clock, reference):
    from companion.life_topics import LifeTopics

    if not isinstance(reference, dict) or set(reference) != {"job_id", "expected_content_sha256"}:
        raise CoreError("LIFE_DRAFT_CHANGED", 409)
    data = LifeTopics(store, settings, clock).retained_result(
        reference["job_id"],
        destination="PRIVATE_CREATION",
    )
    if paid_budget.sha({"text": data["result"]["text"]}) != reference["expected_content_sha256"]:
        raise CoreError("LIFE_DRAFT_CHANGED", 409)
    return data


def _life_sources(store, data):
    sources = sorted({data["job"]["event_id"], *[x["event_id"] for x in data["sources"]]})
    return store.rows(
        'WITH RECURSIVE roots(event_id) AS (SELECT value COLLATE "C" FROM '
        "JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS parsed(value) UNION SELECT "
        "d.source_event_id FROM event_dependencies AS d JOIN roots AS r ON d.event_id = "
        "r.event_id) SELECT e.event_id, e.ingested_at FROM events AS e JOIN roots USING (event_id)",
        (encode(sources),),
    )


def life_record(store, settings, clock, row, snapshot):
    """Read-only native source proof for a Life-origin record, shared with cognition."""
    if set(snapshot) != {"life"}:
        raise CoreError("LIFE_DRAFT_CHANGED", 409)
    data = _life_draft(store, settings, clock, snapshot["life"])
    job = store.one(
        "SELECT idempotency_key, authority_json FROM jobs WHERE job_id = %s AND kind = 'rem'",
        (row["job_id"],),
    )
    expected = {
        "kind": "FICTION",
        "decision": "DREAM",
        "title": "私人创作",
        "text": data["result"]["text"],
        "questions": [],
    }
    if (
        not job
        or job["idempotency_key"] != "life:rem:" + data["job"]["job_id"]
        or json.loads(job["authority_json"]) != json.loads(data["job"]["authority_json"])
        or json.loads(row["source_memory_ids_json"]) != []
        or row["model_request_id"] is not None
        or sorted(json.loads(row["source_event_ids_json"]))
        != sorted(x["event_id"] for x in _life_sources(store, data))
        or json.loads(row["output_json"]) not in ({}, expected)
    ):
        raise CoreError("LIFE_DRAFT_CHANGED", 409)
    return data


def consume_life_draft(store, settings, clock, life_job_id, body):
    """Save exact paid Life prose through REM custody; no new generation or grant."""
    from companion.life_topics import _paused, _uncertain

    if not store.in_transaction:
        raise RuntimeError("REM_ADMISSION_TRANSACTION_REQUIRED")
    view = _authority(store, settings, clock, authority=body.authority)
    reference = {"job_id": life_job_id, "expected_content_sha256": body.expected_content_sha256}
    data = _life_draft(store, settings, clock, reference)
    if settings.life_loop.mode != "SHADOW" or _paused(store, settings, clock()):
        raise CoreError("LIFE_CONSUMPTION_PAUSED", 409)
    if _uncertain(store, settings):
        raise CoreError("LIFE_RECONCILIATION_REQUIRED", 503)
    if budget.chat_pending(store, clock()) or owner_authority.owner_waiting(
        store,
        settings,
        clock(),
        authority=view.authority.as_dict(),
    ):
        raise CoreError("BACKGROUND_YIELD_TO_CHAT", 409)
    key = "life:rem:" + life_job_id
    existing = store.one(
        "SELECT j.job_id, r.rem_id, j.state FROM jobs AS j JOIN rem_records AS r "
        "USING (job_id) WHERE j.kind = 'rem' AND j.idempotency_key = %s",
        (key,),
    )
    if existing:
        return {"action": "rem.save", **existing}
    local = datetime.fromisoformat(clock()).astimezone(ZoneInfo(settings.timezone))
    if local.hour < settings.rem.local_hour:
        raise CoreError("REM_NOT_DUE", 409)
    day = local.date().isoformat()
    if store.one(
        "SELECT 1 FROM rem_records WHERE local_date = %s AND timezone = %s",
        (day, settings.timezone),
    ):
        raise CoreError("REM_DAILY_LIMIT", 429)
    retention = min(
        settings.rem.source_retention_days,
        view.policy.source_retention_days,
        settings.raw_event_retention_days,
    )
    roots = _life_sources(store, data)
    expires = min(
        after(clock(), settings.rem.record_retention_days * 86400),
        data["topic"]["expires_at"],
        *(after(row["ingested_at"], retention * 86400) for row in roots),
    )
    if expires <= clock():
        raise CoreError("LIFE_DRAFT_EXPIRED", 409)
    job_id = _enqueue(
        store,
        settings,
        clock,
        day,
        data["job"]["conversation_id"],
        expires,
        {"life": reference},
        [row["event_id"] for row in roots],
        idempotency_key=key,
    )
    store.audit(
        "life.draft_consumed",
        life_job_id,
        life_job_id,
        clock(),
        channel_action="rem.save",
        rem_job_id=job_id,
    )
    return {
        "action": "rem.save",
        "job_id": job_id,
        "state": "QUEUED",
        "rem_id": store.one("SELECT rem_id FROM rem_records WHERE job_id = %s", (job_id,))[
            "rem_id"
        ],
    }


def check_job(store, settings, clock, job_id, *, lease_token=None):
    row = store.one(
        "SELECT r.rem_id AS rem_id, r.job_id AS job_id, r.local_date AS local_date, "
        "r.timezone AS timezone, r.config_sha256 AS config_sha256, r.source_memory_ids_json AS "
        "source_memory_ids_json, r.source_event_ids_json AS source_event_ids_json, "
        "r.source_snapshot_json AS source_snapshot_json, r.output_json AS output_json, "
        "r.model_request_id AS model_request_id, r.redacted AS redacted, r.created_at AS "
        "created_at, r.expires_at AS expires_at, j.state AS state, j.lease_token AS lease_token, "
        "j.lease_until AS lease_until, j.event_id AS event_id FROM rem_records AS r JOIN jobs AS j "
        "ON r.job_id = j.job_id WHERE r.job_id = %s AND j.kind = 'rem'",
        (job_id,),
    )
    if (
        not row
        or row["redacted"]
        or row["state"] != "RUNNING"
        or not row["lease_until"]
        or row["lease_until"] <= clock()
        or row["expires_at"] <= clock()
        or lease_token is not None
        and row["lease_token"] != lease_token
    ):
        raise CoreError("REM_JOB_FENCED", 409)
    view = owner_authority.check_job(store, settings, clock, row, purpose="rem", executing=True)
    _authority(store, settings, clock, authority=view.authority)
    if row["config_sha256"] != paid_budget.sha(settings.rem.model_dump()):
        raise CoreError("REM_POLICY_CHANGED", 409)
    reply_revision.check_sources(store, job_id)
    snapshot = json.loads(row["source_snapshot_json"])
    if "life" in snapshot:
        from companion.life_topics import _paused, _uncertain

        life_record(store, settings, clock, row, snapshot)
        if settings.life_loop.mode != "SHADOW" or _paused(store, settings, clock()):
            raise CoreError("LIFE_CONSUMPTION_PAUSED", 409)
        if _uncertain(store, settings):
            raise CoreError("LIFE_RECONCILIATION_REQUIRED", 503)
        if budget.chat_pending(store, clock()) or owner_authority.owner_waiting(
            store,
            settings,
            clock(),
            authority=view.authority.as_dict(),
        ):
            raise CoreError("BACKGROUND_YIELD_TO_CHAT", 409)
    else:
        _context(store, settings).revalidate(snapshot["proof"], clock())
    return row, snapshot


def admit(store, settings, clock, job_id, estimated_input, estimate):
    """Called inside original budget.reserve; original usage is the only counter."""
    if not store.in_transaction:
        raise RuntimeError("REM_ADMISSION_TRANSACTION_REQUIRED")
    _, snapshot = check_job(store, settings, clock, job_id)
    if "life" in snapshot:
        raise CoreError("REM_ALREADY_ATTEMPTED", 409)
    cfg = settings.rem
    if estimated_input > cfg.max_input_tokens:
        raise CoreError("REM_INPUT_LIMIT", 429)
    local = datetime.fromisoformat(clock()).astimezone(ZoneInfo(settings.timezone)).date()
    day = day_bounds(local, settings.timezone)[0]
    month = day_bounds(local.replace(day=1), settings.timezone)[0]
    usage = store.one(
        (
            "SELECT COUNT(*) n,COALESCE(SUM(billed_micro_usd),0) "
            "cost FROM model_usage WHERE scenario='rem' AND "
            "occurred_at>=%s"
        ),
        (day,),
    )
    monthly = store.one(
        (
            "SELECT COALESCE(SUM(billed_micro_usd),0) cost FROM "
            "model_usage WHERE scenario='rem' AND occurred_at>=%s"
        ),
        (month,),
    )["cost"]
    if not cfg.daily_micro_usd or not cfg.monthly_micro_usd:
        raise CoreError("SCENARIO_BUDGET_DISABLED", 429)
    if usage["n"] >= 1:
        raise CoreError("SCENARIO_CALL_LIMIT", 429)
    if usage["cost"] + estimate > cfg.daily_micro_usd:
        raise CoreError("DAILY_BUDGET_EXHAUSTED", 429)
    if monthly + estimate > cfg.monthly_micro_usd:
        raise CoreError("SCENARIO_MONTHLY_BUDGET_EXHAUSTED", 429)
    if store.one("SELECT 1 FROM model_usage WHERE job_id = %s", (job_id,)):
        raise CoreError("REM_ALREADY_ATTEMPTED", 409)


async def run(core, job):
    store, settings, clock = core.store, core.settings, core.clock

    def source_check():
        return check_job(store, settings, clock, job["job_id"], lease_token=job["lease_token"])

    try:
        row, snapshot = source_check()
        if "life" in snapshot:
            with store.transaction():
                source_check()
                data = _life_draft(store, settings, clock, snapshot["life"])
                output = {
                    "kind": "FICTION",
                    "decision": "DREAM",
                    "title": "私人创作",
                    "text": data["result"]["text"],
                    "questions": [],
                }
                store.execute(
                    "UPDATE rem_records SET output_json = %s WHERE rem_id = %s",
                    (encode(output), row["rem_id"]),
                )
                store.execute(
                    "UPDATE jobs SET state = 'SUCCEEDED', lease_token = NULL, updated_at "
                    "= %s WHERE job_id = %s AND lease_token = %s",
                    (clock(), job["job_id"], job["lease_token"]),
                )
                store.audit(
                    "rem.completed",
                    row["rem_id"],
                    job["trace_id"],
                    clock(),
                    kind="FICTION",
                    decision="DREAM",
                    life_job_id=data["job"]["job_id"],
                )
            return True
        messages = [
            {"role": "system", "content": RULES},
            {"role": "user", "content": encode({"memories": snapshot["inputs"]})},
        ]
        request_id = budget.reserve(
            store,
            settings,
            job["job_id"],
            "rem",
            messages,
            clock(),
            clock=clock,
            logical_request_id=row["rem_id"],
            source_check=source_check,
        )
        with store.transaction():
            source_check()
            store.execute(
                "UPDATE rem_records SET model_request_id = %s WHERE rem_id = %s",
                (request_id, row["rem_id"]),
            )
        response = await budget.complete(
            core.model,
            store,
            settings,
            request_id,
            messages,
            "rem",
            clock,
            source_check=source_check,
        )
        usage = store.one("SELECT state FROM model_usage WHERE request_id = %s", (request_id,))
        if not usage or usage["state"] != "SETTLED":
            raise CoreError("MODEL_USAGE_UNRESOLVED", 409)
        if response.finish_reason != "stop":
            raise CoreError("REM_INCOMPLETE_RESPONSE", 502)
        draft = RemDraft.model_validate_json(response.text)
        if sensitive(response.text) or redact(response.text, settings) != response.text:
            raise CoreError("REM_SENSITIVE_CONTENT", 400)
        output = {
            "kind": "FICTION",
            "decision": draft.decision,
            "title": draft.title,
            "text": draft.text,
            "questions": [{"text": text, "state": "UNVERIFIED"} for text in draft.questions],
        }
        with store.transaction():
            source_check()
            store.execute(
                "UPDATE rem_records SET output_json = %s WHERE rem_id = %s",
                (encode(output), row["rem_id"]),
            )
            store.execute(
                "UPDATE jobs SET state = 'SUCCEEDED', lease_token = NULL, updated_at = "
                "%s WHERE job_id = %s AND lease_token = %s",
                (clock(), job["job_id"], job["lease_token"]),
            )
            store.audit(
                "rem.completed",
                row["rem_id"],
                job["trace_id"],
                clock(),
                kind="FICTION",
                decision=draft.decision,
            )
    except Exception as error:
        code = error.code if isinstance(error, CoreError) else "REM_INVALID_RESULT"
        with store.transaction():
            if code in budget.CAPACITY_WAIT_CODES and not store.one(
                "SELECT 1 FROM model_usage WHERE job_id = %s",
                (job["job_id"],),
            ):
                store.execute(
                    "UPDATE jobs SET state = 'RETRY_WAIT', lease_token = NULL, "
                    "updated_at = %s, attempts = GREATEST(0, attempts - 1), next_attempt_at = %s, "
                    "last_error_code = %s WHERE job_id = %s AND state = 'RUNNING' AND lease_token "
                    "= %s",
                    (clock(), after(clock(), 60), code, job["job_id"], job["lease_token"]),
                )
                return True
            store.execute(
                "UPDATE jobs SET state = 'FAILED', lease_token = NULL, last_error_code = "
                "%s, updated_at = %s WHERE job_id = %s AND state = 'RUNNING' AND lease_token = %s",
                (code, clock(), job["job_id"], job["lease_token"]),
            )
            store.audit("rem.failed", job["job_id"], job["trace_id"], clock(), code=code)
    return True


def recover_interrupted(store, now):
    store.execute(
        "UPDATE jobs SET state = 'CANCELLED', lease_token = NULL, updated_at = %s, "
        "last_error_code = 'REM_INTERRUPTED' WHERE kind = 'rem' AND state = 'RUNNING' AND "
        "lease_until <= %s",
        (now, now),
    )


def erase(store, event_ids):
    if not event_ids or not store.one(
        "SELECT 1 FROM pg_catalog.pg_class WHERE "
        "relnamespace=current_schema()::regnamespace AND relname='rem_records' AND relkind IN "
        "('r','p')"
    ):
        return
    keys = encode(sorted(event_ids))
    for row in store.rows(
        (
            (
                'SELECT r.rem_id,r.job_id FROM rem_records r JOIN jobs '
                'j USING(job_id) WHERE r.redacted=0 AND (j.event_id IN '
                '(SELECT value FROM json_array_elements_text(%s::json) '
                'AS ids(value)) OR EXISTS (SELECT 1 FROM '
                'json_array_elements_text(r.source_event_ids_json::json)'
                ' AS s(value) WHERE s.value IN (SELECT value FROM '
                'json_array_elements_text(%s::json) AS ids(value))))'
            )
        ),
        (keys, keys),
    ):
        _scrub(store, row, "REM_SOURCE_ERASED")


def _scrub(store, row, code):
    store.execute(
        "UPDATE rem_records SET redacted = 1, source_snapshot_json = '{}', output_json = "
        "'{}', source_memory_ids_json = '[]', source_event_ids_json = '[]' WHERE rem_id = %s",
        (row["rem_id"],),
    )
    store.execute(
        "UPDATE jobs SET state = 'CANCELLED', lease_token = NULL, last_error_code = %s "
        "WHERE job_id = %s AND state IN ('QUEUED', 'RUNNING', 'RETRY_WAIT')",
        (code, row["job_id"]),
    )


def expire(store, ledger, now):
    if not store.one(
        "SELECT 1 FROM pg_catalog.pg_class WHERE "
        "relnamespace=current_schema()::regnamespace AND relname='rem_records' AND relkind IN "
        "('r','p')"
    ):
        return
    rows = store.rows(
        "SELECT j.event_id FROM rem_records AS r JOIN jobs AS j USING (job_id) WHERE "
        "r.redacted = 0 AND r.expires_at <= %s ORDER BY r.expires_at NULLS FIRST LIMIT 1000",
        (now,),
    )
    if rows:
        # Expire only the derived REM event. Original memory sources and their
        # facts remain intact; the original external Ledger makes restore replay safe.
        ledger.expire_events(store, [row["event_id"] for row in rows], now, retention_cutoff=now)


def records(core, *, limit=20):
    """Owner-control projection; outer API authenticates owner, even after REM stops."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise CoreError("INVALID_REM_LIMIT", 400)
    version, result = read_version(core.memory), []
    for row in core.store.rows(
        "SELECT r.rem_id AS rem_id, r.job_id AS job_id, r.local_date AS "
        "local_date, r.timezone AS timezone, r.config_sha256 AS config_sha256, "
        "r.source_memory_ids_json AS source_memory_ids_json, r.source_event_ids_json AS "
        "source_event_ids_json, r.source_snapshot_json AS source_snapshot_json, r.output_json AS "
        "output_json, r.model_request_id AS model_request_id, r.redacted AS redacted, r.created_at "
        "AS created_at, r.expires_at AS expires_at, j.state AS state, j.last_error_code AS "
        "last_error_code FROM rem_records AS r JOIN jobs AS j ON r.job_id = j.job_id ORDER BY "
        "r.created_at DESC NULLS LAST, r.rem_id NULLS FIRST LIMIT %s",
        (limit,),
    ):
        available = not row["redacted"] and row["expires_at"] > core.clock()
        if available:
            try:
                snapshot = json.loads(row["source_snapshot_json"])
                if "life" in snapshot:
                    life_record(core.store, core.settings, core.clock, row, snapshot)
                else:
                    core.memory_context.revalidate(snapshot["proof"], core.clock())
            except (CoreError, ValueError, KeyError):
                available = False
        output = json.loads(row["output_json"]) if available else {}
        usage = core.store.one(
            "SELECT request_id, state, input_tokens, output_tokens, "
            "cached_input_tokens, billed_micro_usd FROM model_usage WHERE job_id = %s AND scenario "
            "= 'rem' ORDER BY record_sequence DESC NULLS LAST LIMIT 1",
            (row["job_id"],),
        )
        result.append(
            {
                **{
                    key: row[key]
                    for key in (
                        "rem_id",
                        "job_id",
                        "local_date",
                        "timezone",
                        "state",
                        "last_error_code",
                        "redacted",
                        "expires_at",
                    )
                },
                "kind": "FICTION",
                "source_available": available,
                "decision": output.get("decision"),
                "title": output.get("title", ""),
                "text": output.get("text", ""),
                "questions": output.get("questions", []),
                "source_memory_ids": json.loads(row["source_memory_ids_json"]) if available else [],
                "source_event_ids": json.loads(row["source_event_ids_json"]) if available else [],
                "usage": usage,
            }
        )
    check_version(core.memory, version)
    return result
