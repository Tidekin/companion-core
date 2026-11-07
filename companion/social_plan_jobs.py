"""Social pre-admission deferrals on native jobs and their existing lease fields."""

import json
from contextlib import nullcontext

from companion import budget, owner_authority, paid_budget, reply_revision
from companion.contracts import CoreError
from companion.store import after, encode, uid, utcnow

KIND = "social_plan"
RETRY_SECONDS = 60  # Same pre-admission delay as native required activity jobs.


def available(store):
    return bool(store.one('SELECT 1 FROM pg_catalog.pg_class WHERE '
        "relnamespace=current_schema()::regnamespace AND relname='social_autonomy_jobs' AND "
        "relkind IN ('r','p')"))


def get(store, plan_id):
    if not available(store):
        return None
    return store.one('SELECT j.job_id AS job_id, j.event_id AS event_id, j.kind AS kind, j.state '
        'AS state, j.idempotency_key AS idempotency_key, j.attempts AS attempts, j.max_attempts AS '
        'max_attempts, j.lease_owner AS lease_owner, j.lease_until AS lease_until, '
        'j.next_attempt_at AS next_attempt_at, j.last_error_code AS last_error_code, j.created_at '
        'AS created_at, j.updated_at AS updated_at, j.lease_token AS lease_token, '
        'j.context_tracked AS context_tracked, j.authority_json AS authority_json FROM '
        'social_autonomy_jobs AS r JOIN jobs AS j ON r.job_id = j.job_id WHERE r.plan_id = '
        '%s', (plan_id,))


def queue(module, plan_id):
    """Keep one source-bound native job; never revive a cancelled or attempted job."""
    s, now = module.store, module.clock()
    with nullcontext() if s.in_transaction else s.transaction():
        existing = get(s, plan_id)
        if existing:
            return existing
        row = module._validate_plan(plan_id, allow_unadmitted=True)
        if not module.retryable_unadmitted(row):
            return None
        conversation = s.one(
            "SELECT conversation_id FROM conversations WHERE platform = 'qq.onebot' AND scope = "
            "'private' AND owner_actor_id = 'owner' AND platform_session_key = %s",
            ("onebot-direct:FriendMessage:" + module.settings.owner_qq_id,),
        )
        if not conversation:
            return None
        event_id, job_id = uid("evt"), uid("job")
        authority = json.loads(row["input_snapshot_json"]).get("authority", {})
        s.insert("events", event_id=event_id, schema_version=1, source="system.control",
                 source_event_id="social-retry:" + plan_id,
                 conversation_id=conversation["conversation_id"], actor_id="system",
                 event_type="social.plan_retry", occurred_at=now, ingested_at=now,
                 privacy_scope="private",
                 body_json=encode({"plan_id": plan_id, "authority": authority}),
                 trace_id=plan_id, memory_processed=1)
        s.insert("jobs", job_id=job_id, event_id=event_id, kind=KIND, state="QUEUED",
                 idempotency_key="social-retry:" + plan_id, max_attempts=1, context_tracked=1,
                 next_attempt_at=after(now, RETRY_SECONDS), last_error_code=row["last_error_code"],
                 created_at=now, updated_at=now)
        if owner_authority.required(s, module.settings):
            owner_authority.current(s, module.settings, module.clock,
                                    purpose="social", authority=authority)
            owner_authority.bind_job(s, module.settings, module.clock, job_id, purpose="social")
        s.insert("social_autonomy_jobs", plan_id=plan_id, job_id=job_id)
        s.insert("job_sources", job_id=job_id, event_id=event_id)
        for source in s.rows('SELECT event_id FROM social_autonomy_sources WHERE plan_id = %s',
                             (plan_id,)):
            s.insert("job_sources", job_id=job_id, event_id=source["event_id"])
            s.insert("event_dependencies", event_id=event_id, source_event_id=source["event_id"])
        s.audit("social.retry_queued", job_id, plan_id, now, plan_id=plan_id)
        return get(s, plan_id)


def validate_binding(store, settings, job, clock=utcnow):
    """Recheck original plan, source, native lease and current owner authority."""
    from companion.social_autonomy import SocialAutonomy

    cfg = SocialAutonomy(store, settings, clock=clock)._policy()
    if owner_authority.required(store, settings):
        owner_authority.check_job(store, settings, clock, job, purpose="social", executing=True)
    row = store.one(
        'SELECT e.event_id AS event_id, e.schema_version AS schema_version, e.source AS source, '
        'e.source_event_id AS source_event_id, e.conversation_id AS conversation_id, e.actor_id AS '
        'actor_id, e.event_type AS event_type, e.occurred_at AS occurred_at, e.ingested_at AS '
        'ingested_at, e.privacy_scope AS privacy_scope, e.body_json AS body_json, e.trace_id AS '
        'trace_id, e.idempotency_key AS idempotency_key, e.fingerprint AS fingerprint, e.redacted '
        'AS redacted, e.memory_processed AS memory_processed, e.reply_revision AS reply_revision, '
        'c.scope AS scope, c.owner_actor_id AS owner_actor_id, c.platform AS platform, '
        'c.platform_session_key AS platform_session_key, p.plan_id AS plan_id, p.state AS '
        'plan_state, p.redacted AS plan_redacted, p.policy_sha256 AS policy_sha256, p.expires_at '
        'AS plan_expires_at, p.input_snapshot_json AS input_snapshot_json, j.lease_until AS '
        'job_lease_until, j.authority_json AS authority_json FROM social_autonomy_jobs AS r JOIN '
        'social_autonomy_plans AS p ON r.plan_id = p.plan_id JOIN jobs AS j ON r.job_id = j.job_id '
        'JOIN events AS e ON e.event_id = j.event_id JOIN conversations AS c ON e.conversation_id '
        "= c.conversation_id WHERE j.job_id = %s AND j.kind = %s AND j.state = 'RUNNING' AND "
        'j.lease_token = %s AND j.event_id = %s',
        (job["job_id"], KIND, job["lease_token"], job["event_id"]),
    )
    if (not row or row["plan_redacted"] or row["redacted"]
        or row["plan_state"] not in {"PLANNING", "REJECTED"}
        or row["policy_sha256"] != paid_budget.sha(cfg.model_dump())
        or (row["source"], row["actor_id"], row["event_type"], row["privacy_scope"],
            row["scope"], row["owner_actor_id"], row["platform"], row["platform_session_key"])
        != ("system.control", "system", "social.plan_retry", "private", "private", "owner",
            "qq.onebot", "onebot-direct:FriendMessage:" + settings.owner_qq_id)
        or json.loads(row["body_json"]) != {
            "plan_id": row["plan_id"], "authority": json.loads(row["authority_json"]),
        }
        or json.loads(row["input_snapshot_json"]).get("authority", {})
        != json.loads(row["authority_json"])):
        raise CoreError("SOCIAL_RETRY_JOB_FENCED", 409)
    reply_revision.check_sources(store, job["job_id"])
    return row


def claim(module, worker_id):
    s, now = module.store, module.clock()
    with s.transaction():
        if budget.chat_pending(s, now):
            return None
        row = s.one('SELECT j.job_id AS job_id, j.event_id AS event_id, j.kind AS kind, j.state AS '
            'state, j.idempotency_key AS idempotency_key, j.attempts AS attempts, j.max_attempts '
            'AS max_attempts, j.lease_owner AS lease_owner, j.lease_until AS lease_until, '
            'j.next_attempt_at AS next_attempt_at, j.last_error_code AS last_error_code, '
            'j.created_at AS created_at, j.updated_at AS updated_at, j.lease_token AS lease_token, '
            'j.context_tracked AS context_tracked, j.authority_json AS authority_json, r.plan_id '
            'AS plan_id FROM jobs AS j JOIN social_autonomy_jobs AS r ON j.job_id = r.job_id WHERE '
            "j.kind = %s AND j.state IN ('QUEUED', 'RETRY_WAIT') AND (j.next_attempt_at IS NULL OR "
            'j.next_attempt_at <= %s) ORDER BY j.record_sequence NULLS FIRST LIMIT 1', (KIND, now))
        if not row:
            return None
        try:
            if owner_authority.required(s, module.settings):
                owner_authority.check_job(s, module.settings, module.clock, row, purpose="social")
            plan = module._validate_plan(row["plan_id"], allow_unadmitted=True)
            if not module.retryable_unadmitted(plan):
                raise CoreError("SOCIAL_RETRY_ALREADY_ADMITTED", 409)
        except CoreError as error:
            s.execute("UPDATE jobs SET state = 'CANCELLED', lease_token = NULL, last_error_code = "
                '%s, updated_at = %s WHERE job_id = %s', (error.code, now, row["job_id"]))
            return None
        token = uid("lease")
        until = min(plan["expires_at"], after(now, module.settings.lease_seconds))
        s.execute("UPDATE jobs SET state = 'RUNNING', lease_owner = %s, lease_token = %s, "
            'lease_until = %s, attempts = attempts + 1, updated_at = %s WHERE job_id = %s',
                  (worker_id, token, until, now, row["job_id"]))
        return s.one('SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, jobs.kind AS kind, '
            'jobs.state AS state, jobs.idempotency_key AS idempotency_key, jobs.attempts AS '
            'attempts, jobs.max_attempts AS max_attempts, jobs.lease_owner AS lease_owner, '
            'jobs.lease_until AS lease_until, jobs.next_attempt_at AS next_attempt_at, '
            'jobs.last_error_code AS last_error_code, jobs.created_at AS created_at, '
            'jobs.updated_at AS updated_at, jobs.lease_token AS lease_token, jobs.context_tracked '
            'AS context_tracked, jobs.authority_json AS authority_json FROM jobs AS jobs WHERE '
            'jobs.job_id = %s', (row["job_id"],))


def fail(module, job, code):
    """Called in the planner's failure transaction; only unadmitted refusals defer."""
    from companion.social_autonomy import UNADMITTED_RETRY_ERRORS

    s, now = module.store, module.clock()
    row = s.one('SELECT p.plan_id AS plan_id, p.idempotency_key AS idempotency_key, '
        'p.request_sha256 AS request_sha256, p.candidate_sha256 AS candidate_sha256, p.state AS '
        'state, p.policy_version AS policy_version, p.policy_sha256 AS policy_sha256, '
        'p.authorization_sha256 AS authorization_sha256, p.binding_sha256 AS binding_sha256, '
        'p.timezone AS timezone, p.policy_json AS policy_json, p.input_snapshot_json AS '
        'input_snapshot_json, p.source_snapshot_sha256 AS source_snapshot_sha256, p.plan_json AS '
        'plan_json, p.payload_sha256 AS payload_sha256, p.model_request_id AS model_request_id, '
        'p.last_error_code AS last_error_code, p.redacted AS redacted, p.created_at AS created_at, '
        'p.expires_at AS expires_at, p.updated_at AS updated_at FROM social_autonomy_jobs AS r '
        'JOIN social_autonomy_plans AS p ON r.plan_id = p.plan_id WHERE r.job_id = '
        '%s', (job["job_id"],))
    retry = code in UNADMITTED_RETRY_ERRORS and module.retryable_unadmitted(row)
    state = "RETRY_WAIT" if retry else "FAILED"
    changed = s.execute(
        'UPDATE jobs SET state = %s, lease_token = NULL, next_attempt_at = %s, last_error_code = '
        '%s, attempts = CASE WHEN %s THEN GREATEST(0, attempts - 1) ELSE attempts END, updated_at '
        "= %s WHERE job_id = %s AND state = 'RUNNING' AND lease_token = %s",
        (state, after(now, RETRY_SECONDS) if retry else None, code, bool(retry), now,
         job["job_id"], job["lease_token"]),
    )
    if changed:
        s.audit("social.retry_deferred" if retry else "social.retry_failed", job["job_id"],
                row["plan_id"], now, error_code=code)


def recover(store, now):
    """Reclaim only expired unadmitted leases; paid/unknown attempts never requeue."""
    if not available(store):
        return False
    from companion.social_autonomy import UNADMITTED_RETRY_ERRORS

    rows = store.rows(
        'SELECT j.job_id AS job_id, j.event_id AS event_id, j.kind AS kind, j.state AS state, '
        'j.idempotency_key AS idempotency_key, j.attempts AS attempts, j.max_attempts AS '
        'max_attempts, j.lease_owner AS lease_owner, j.lease_until AS lease_until, '
        'j.next_attempt_at AS next_attempt_at, j.last_error_code AS last_error_code, j.created_at '
        'AS created_at, j.updated_at AS updated_at, j.lease_token AS lease_token, '
        'j.context_tracked AS context_tracked, j.authority_json AS authority_json, p.plan_id AS '
        'plan_id, p.state AS plan_state, p.expires_at AS expires_at, p.redacted AS redacted, '
        'p.last_error_code AS plan_error, m.state AS usage_state, a.lease_until AS model_lease, '
        'a.request_id AS admission_id, p.model_request_id AS model_request_id FROM jobs AS j JOIN '
        'social_autonomy_jobs AS r ON j.job_id = r.job_id JOIN social_autonomy_plans AS p ON '
        'r.plan_id = p.plan_id LEFT JOIN model_usage AS m ON m.request_id = p.model_request_id '
        'LEFT JOIN model_admissions AS a ON a.request_id = p.model_request_id WHERE j.kind = %s '
        "AND j.state IN ('QUEUED', 'RETRY_WAIT', 'RUNNING') AND (p.expires_at <= %s OR p.redacted "
        "= 1 OR p.state = 'CANCELLED' OR (j.state = 'RUNNING' AND j.lease_until <= %s)) ORDER BY "
        'j.record_sequence NULLS FIRST LIMIT 64',
        (KIND, now, now),
    )
    changed = False
    for row in rows:
        if (row["model_lease"] and row["model_lease"] > now
            or row["state"] == "RUNNING" and row["lease_until"] and row["lease_until"] > now):
            continue
        unadmitted = (row["usage_state"] is None and row["admission_id"] is None
                      and not store.one('SELECT 1 FROM cloud_cost_snapshots WHERE reference_type = '
                          "'model' AND reference_id = %s",
                                        (row["model_request_id"],)))
        retry = (unadmitted and not row["redacted"] and row["expires_at"] > now
                 and row["plan_state"] == "REJECTED"
                 and row["plan_error"] in UNADMITTED_RETRY_ERRORS)
        state = "RETRY_WAIT" if retry else "CANCELLED"
        store.execute('UPDATE jobs SET state = %s, lease_token = NULL, next_attempt_at = %s, '
            'attempts = CASE WHEN %s THEN GREATEST(0, attempts - 1) ELSE attempts END, '
            "last_error_code = 'SOCIAL_RETRY_LEASE_EXPIRED', updated_at = %s WHERE job_id = %s",
                      (state, after(now, RETRY_SECONDS) if retry else None, bool(retry), now,
                       row["job_id"]))
        store.audit("social.retry_recovered", row["job_id"], row["plan_id"], now, state=state)
        changed = True
    return changed


def cancel_plans(store, plan_ids, reason, now):
    if available(store):
        store.execute("UPDATE jobs SET state = 'CANCELLED', lease_token = NULL, last_error_code = "
            "%s, updated_at = %s WHERE state IN ('QUEUED', 'RUNNING', 'RETRY_WAIT') AND job_id IN "
            '(SELECT job_id FROM social_autonomy_jobs WHERE plan_id IN (SELECT value COLLATE "C" '
            'FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS '
            'parsed(value)))', (reason, now, encode(list(plan_ids))))
