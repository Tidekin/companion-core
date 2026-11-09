"""One background decision using retained public evidence and the native Outbox.

The Life selector calls this between bounded native units. This module never fetches
account data or sends an action; reviewed assets and real profile probes arrive
through the authenticated evidence interfaces on SocialAutonomy.
"""

import hashlib
import json

from companion import budget, social_plan_jobs, social_profile_sources
from companion.contracts import CoreError
from companion.social_autonomy import SocialAutonomy, public_source
from companion.store import after, encode, utcnow

_SCAN_LIMIT = 64
_INVALID_PLAN = {
    "SOCIAL_PLAN_UNAVAILABLE", "SOCIAL_PLAN_CHANGED", "SOCIAL_PLAN_AUTHORITY_CHANGED",
    "SOCIAL_SOURCE_CHANGED", "SOCIAL_SOURCE_EXPIRED", "SOCIAL_PUBLIC_SOURCE_REQUIRED",
    "SOCIAL_PRIVATE_OR_INVALID_TEXT", "SOCIAL_REVIEWED_AVATAR_REQUIRED",
    "SOCIAL_PROFILE_BASELINE_REQUIRED", "SOCIAL_PROFILE_BASELINE_STALE",
    "SOCIAL_DUPLICATE_ACTION", "SOCIAL_PROFILE_PURPOSE_REQUIRED",
    "SOCIAL_PROFILE_SOURCE_REQUIRED", "PRIVACY_CHANGED",
    "OWNER_AUTHORITY_REVISION_CHANGED", "OWNER_AUTHORITY_REFERENCE_INVALID",
    "SOCIAL_DELIVERY_AUTHORITY_CHANGED",
}


def _fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def _source_key(source):
    # A fresh receipt or a new event ID cannot turn the same input into a retry.
    return _fingerprint([source["url"], source["title"], source["source_sha256"],
                         source["coverage"]])


def _fits(module, sources, assets, profiles):
    try:
        module._messages({"sources": sources, "assets": assets, "profiles": profiles},
                         module.config)
    except CoreError as error:
        if error.code != "SOCIAL_CONTEXT_TOO_LARGE":
            raise
        return False
    return True


def _candidates(module):
    cfg, s, now = module.config, module.store, module.clock()
    sources, seen = [], set()
    for row in s.rows(
        'SELECT o.event_id FROM observations AS o JOIN events AS e USING (event_id) WHERE '
        "o.result_status = 'FETCHED' AND o.source_kind = 'text' AND o.redacted = 0 AND e.redacted "
        "= 0 AND e.event_type = 'content.observed' AND e.source IN ('observation.bilibili', "
        "'observation.heybox', 'observation.web') AND e.ingested_at >= %s AND e.occurred_at <= %s "
        'ORDER BY e.ingested_at DESC NULLS LAST, o.event_id DESC NULLS LAST LIMIT %s',
        (after(now, -86400 * module.settings.raw_event_retention_days), now, _SCAN_LIMIT),
    ):
        try:
            source = public_source(s, module.settings, row["event_id"], now)
        except CoreError:
            continue
        key = _source_key(source)
        if key not in seen:
            sources.append(source)
            seen.add(key)
        if len(sources) >= cfg.max_source_refs:
            break
    profiles, assets = [], []
    for field in ("avatar",):
        if not getattr(cfg, field + "_enabled"):
            continue
        try:
            profiles.extend(module.context(profile_fields=[field])["profiles"])
        except CoreError:
            continue
    if any(profile["field"] == "avatar" for profile in profiles):
        for row in s.rows('SELECT asset_id FROM social_autonomy_assets WHERE revoked_at IS NULL '
            'AND expires_at > %s ORDER BY created_at DESC NULLS LAST, asset_id NULLS FIRST LIMIT '
            '%s',
                          (now, _SCAN_LIMIT)):
            try:
                assets.extend(module.context(asset_ids=[row["asset_id"]])["assets"])
            except CoreError:
                continue
            if len(assets) >= 20:
                break
    while not _fits(module, [], assets, profiles):
        if assets:
            assets.pop()
        elif profiles:
            profiles.pop()
        else:
            return None
    if not assets:
        profiles = [profile for profile in profiles if profile["field"] != "avatar"]
    fitted = []
    for source in sources:
        if _fits(module, [*fitted, source], assets, profiles):
            fitted.append(source)
    sources = fitted
    if not (cfg.qzone_enabled and sources or profiles):
        return None
    semantic = {
        "sources": sorted(_source_key(source) for source in sources),
        "profiles": [{"field": p["field"], "value": p["value"]} for p in profiles],
        "assets": sorted((_fingerprint({key: a[key] for key in
                                      ("asset_id", "asset_sha256", "description")}))
                         for a in assets),
    }
    return "social-runtime:" + _fingerprint(semantic), {
        "source_ids": sorted(source["ref"] for source in sources),
        "asset_ids": sorted(asset["asset_id"] for asset in assets),
        "profile_fields": sorted(profile["field"] for profile in profiles),
    }


def _profile_candidate(module):
    fields = []
    for field in ("nickname", "signature"):
        if not getattr(module.config, field + "_enabled"):
            continue
        try:
            module.context(profile_fields=[field])
        except CoreError:
            continue
        fields.append(field)
    if not fields:
        return None
    ids = social_profile_sources.candidates(module.store, module.settings, module.clock())
    while ids:
        context = module.context(profile_fields=fields, profile_memory_ids=ids)
        try:
            module._messages(context, module.config)
        except CoreError as error:
            if error.code != "SOCIAL_CONTEXT_TOO_LARGE":
                raise
            ids.pop()
            continue
        semantic = {"purpose": context["purpose"], "profile_material": context["profile_material"],
                    "profiles": [{"field": p["field"], "value": p["value"]}
                                 for p in context["profiles"]]}
        return "social-profile:" + _fingerprint(semantic), {
            "profile_fields": fields, "profile_memory_ids": ids,
        }
    return None


def _cancel(store, plan_id, code, now):
    store.execute("UPDATE social_autonomy_plans SET state = 'CANCELLED', last_error_code = %s, "
        "updated_at = %s WHERE plan_id = %s AND state = 'READY' AND NOT EXISTS(SELECT 1 FROM "
        'social_autonomy_reservations AS r WHERE r.plan_id = %s)',
                  (code, now, plan_id, plan_id))
    store.audit("social.runtime_cancelled", plan_id, plan_id, now, error_code=code)


def _expire(store, now):
    with store.transaction():
        recovered_jobs = social_plan_jobs.recover(store, now)
        rows = store.rows('SELECT p.plan_id FROM social_autonomy_plans AS p WHERE p.state = '
            "'READY' AND p.expires_at <= %s AND NOT EXISTS(SELECT 1 FROM "
            'social_autonomy_reservations AS r WHERE r.plan_id = p.plan_id) ORDER BY p.expires_at '
            'NULLS FIRST, p.plan_id NULLS FIRST LIMIT %s', (now, _SCAN_LIMIT))
        for row in rows:
            _cancel(store, row["plan_id"], "SOCIAL_PLAN_EXPIRED", now)
        abandoned = store.rows(
            'SELECT p.plan_id, m.state AS usage_state FROM social_autonomy_plans AS p LEFT JOIN '
            'model_usage AS m ON m.request_id = p.model_request_id LEFT JOIN model_admissions AS a '
            "ON a.request_id = p.model_request_id WHERE p.state = 'PLANNING' AND p.expires_at <= "
            "%s AND (m.request_id IS NULL OR (m.state = 'SETTLED' AND m.scenario = "
            "'social_autonomy')) AND (a.request_id IS NULL OR a.lease_until <= %s) AND NOT "
            'EXISTS(SELECT 1 FROM social_autonomy_reservations AS r WHERE r.plan_id = p.plan_id) '
            'ORDER BY p.expires_at NULLS FIRST, p.plan_id NULLS FIRST LIMIT %s',
            (now, now, _SCAN_LIMIT),
        )
        for row in abandoned:
            code = ("SOCIAL_PLAN_RESULT_LOST" if row["usage_state"] == "SETTLED"
                    else "SOCIAL_PLAN_EXPIRED_UNADMITTED")
            store.execute("UPDATE social_autonomy_plans SET state = 'CANCELLED', last_error_code = "
                "%s, updated_at = %s WHERE plan_id = %s AND state = 'PLANNING'",
                          (code, now, row["plan_id"]))
            store.audit("social.runtime_cancelled", row["plan_id"], row["plan_id"], now,
                        error_code=code)
            social_plan_jobs.cancel_plans(store, [row["plan_id"]], code, now)
    return bool(rows or abandoned or recovered_jobs)


def _enqueue(module, plan_id, conversation):
    with module.store.transaction():
        if budget.chat_pending(module.store, module.clock()):
            return False
        try:
            module.reserve(plan_id, conversation)
        except CoreError as error:
            if error.code not in _INVALID_PLAN:
                return False
            _cancel(module.store, plan_id, error.code, module.clock())
        return True


async def run_once(store, settings, model, clock=utcnow, *, worker_id="social-worker") -> bool:
    """Return True only after durable work; False means idle, yielded or fenced.

    No caller transaction may span this call's model await. Invalid or expired
    READY plans are cancelled; transient quota/authority fences preserve them.
    Only proven pre-admission budget refusals can reuse their original request.
    Paid, uncertain, skipped, interrupted and delivered candidates are never retried.
    """
    module = SocialAutonomy(store, settings, model, clock)
    if not module.config.enabled:
        return False
    changed = _expire(store, clock())
    if budget.chat_pending(store, clock()):
        return changed
    try:
        module._authorized()
    except CoreError:
        return changed
    conversation = store.one(
        "SELECT conversation_id FROM conversations WHERE platform = 'qq.onebot' AND scope = "
        "'private' AND owner_actor_id = 'owner' AND platform_session_key = %s",
        ("onebot-direct:FriendMessage:" + settings.owner_qq_id,),
    )
    if not conversation:
        return changed
    conversation = conversation["conversation_id"]
    ready = store.one(
        'SELECT p.plan_id FROM social_autonomy_plans p WHERE '
        "p.state='READY' AND p.redacted=0 AND NOT EXISTS "
        '(SELECT 1 FROM social_autonomy_reservations r WHERE '
        'r.plan_id=p.plan_id) AND (CASE WHEN p.plan_json IS '
        "JSON THEN ((p.plan_json::json#>>'{schema_version}'))::n"
        'umeric END)<>2 ORDER BY p.created_at,p.plan_id LIMIT 1'
    )
    if ready:
        return _enqueue(module, ready["plan_id"], conversation) or changed
    if model is None:
        return changed
    retry = social_plan_jobs.claim(module, worker_id)
    if retry:
        row = store.one('SELECT p.plan_id AS plan_id, p.idempotency_key AS idempotency_key, '
            'p.request_sha256 AS request_sha256, p.candidate_sha256 AS candidate_sha256, p.state '
            'AS state, p.policy_version AS policy_version, p.policy_sha256 AS policy_sha256, '
            'p.authorization_sha256 AS authorization_sha256, p.binding_sha256 AS binding_sha256, '
            'p.timezone AS timezone, p.policy_json AS policy_json, p.input_snapshot_json AS '
            'input_snapshot_json, p.source_snapshot_sha256 AS source_snapshot_sha256, p.plan_json '
            'AS plan_json, p.payload_sha256 AS payload_sha256, p.model_request_id AS '
            'model_request_id, p.last_error_code AS last_error_code, p.redacted AS redacted, '
            'p.created_at AS created_at, p.expires_at AS expires_at, p.updated_at AS updated_at '
            'FROM social_autonomy_jobs AS r JOIN social_autonomy_plans AS p ON r.plan_id = '
            'p.plan_id WHERE r.job_id = %s', (retry["job_id"],))
        try:
            messages, _ = module._messages(json.loads(row["input_snapshot_json"]), module.config)
            plan = await module._run_plan(row["plan_id"], row["model_request_id"], messages,
                                          job=retry)
        except Exception as error:
            code = error.code if isinstance(error, CoreError) else "SOCIAL_RETRY_FAILED"
            with store.transaction():
                social_plan_jobs.fail(module, retry, code)
            return True
        if plan["state"] == "READY":
            _enqueue(module, plan["plan_id"], conversation)
        return True
    candidate = None
    builders = (_profile_candidate, _candidates) if settings.life_loop.mode == "OFF" else (
        _profile_candidate,
    )
    for build in builders:
        try:
            candidate = build(module)
        except CoreError:
            continue
        if not candidate:
            continue
        key, inputs = candidate
        previous = store.one('SELECT social_autonomy_plans.plan_id AS plan_id, '
            'social_autonomy_plans.idempotency_key AS idempotency_key, '
            'social_autonomy_plans.request_sha256 AS request_sha256, '
            'social_autonomy_plans.candidate_sha256 AS candidate_sha256, '
            'social_autonomy_plans.state AS state, social_autonomy_plans.policy_version AS '
            'policy_version, social_autonomy_plans.policy_sha256 AS policy_sha256, '
            'social_autonomy_plans.authorization_sha256 AS authorization_sha256, '
            'social_autonomy_plans.binding_sha256 AS binding_sha256, '
            'social_autonomy_plans.timezone AS timezone, social_autonomy_plans.policy_json AS '
            'policy_json, social_autonomy_plans.input_snapshot_json AS input_snapshot_json, '
            'social_autonomy_plans.source_snapshot_sha256 AS source_snapshot_sha256, '
            'social_autonomy_plans.plan_json AS plan_json, social_autonomy_plans.payload_sha256 AS '
            'payload_sha256, social_autonomy_plans.model_request_id AS model_request_id, '
            'social_autonomy_plans.last_error_code AS last_error_code, '
            'social_autonomy_plans.redacted AS redacted, social_autonomy_plans.created_at AS '
            'created_at, social_autonomy_plans.expires_at AS expires_at, '
            'social_autonomy_plans.updated_at AS updated_at FROM social_autonomy_plans AS '
            'social_autonomy_plans WHERE social_autonomy_plans.idempotency_key = %s', (key,))
        if not previous:
            break
        if (module.retryable_unadmitted(previous)
            and social_plan_jobs.get(store, previous["plan_id"]) is None):
            try:
                return bool(social_plan_jobs.queue(module, previous["plan_id"])) or changed
            except CoreError:
                pass
        candidate = None
    if not candidate:
        return changed
    try:
        plan = await module.plan(key, **inputs)
    except CoreError:
        return bool(store.one('SELECT 1 FROM social_autonomy_plans WHERE idempotency_key = %s',
                              (key,))) or changed
    except Exception:
        # plan() has already recorded the failure and native fee/uncertainty.
        # Only absorb a failure with that durable record; programming errors
        # before a decision exists must remain visible to the worker.
        if store.one('SELECT 1 FROM social_autonomy_plans WHERE idempotency_key = %s', (key,)):
            return True
        raise
    if plan["state"] == "READY":
        _enqueue(module, plan["plan_id"], conversation)
    return plan["state"] != "PLANNING" or changed
