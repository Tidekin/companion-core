"""Read-only capability facts shared by the reply prompt and operator views.

READY describes local admission readiness, never a completed action or proof that
an upstream service is reachable. Actual workflows still enforce their leases,
source checks and budgets at dispatch. No guard that writes state is called here.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from companion import (
    cloud_budget,
    cumulative,
    delegation,
    live_text,
    model_carry,
    owner_authority,
    paid_budget,
    private_life,
    qq_images,
    video_input,
)
from companion.budget import capacity_exhausted, total_monthly
from companion.contracts import CoreError
from companion.exploration import METERED_USAGE_SQL
from companion.source_grants import SourceGrantService, SourceGrantSnapshot
from companion.sources import supports


def _meta(store, key):
    row = store.one("SELECT value FROM runtime_meta WHERE key = %s", (key,))
    return row["value"] if row else None


def _observation(facts, now):
    if not isinstance(facts, dict):
        return "UNOBSERVED", {}
    try:
        observed = datetime.fromisoformat(facts["observed_at"])
        expires = datetime.fromisoformat(facts["expires_at"])
        current = datetime.fromisoformat(now)
        if observed.tzinfo is None or expires.tzinfo is None or not observed <= current:
            return "UNOBSERVED", {}
        if not observed < expires:
            return "UNOBSERVED", {}
        return ("OBSERVED", facts) if current < expires else ("STALE", {})
    except (KeyError, TypeError, ValueError):
        return "UNOBSERVED", {}


def _carry_view(store, settings):
    """Reuse verified native closure; an absent/invalid proof grants no exception."""
    if not owner_authority.required(store, settings) or not model_carry.active(settings):
        return {}
    try:
        model_carry.supported(store, settings)
        return paid_budget.journal(store, settings.paid_model, initialize=False)
    except CoreError:
        return {}


def open_unknown(store, settings):
    """Whether an UNKNOWN call still holds a provider slot, counted as admission counts it."""
    closed = _carry_view(store, settings).get("model_closures", {})
    return any(
        row["request_id"] not in closed
        for row in store.rows("SELECT request_id FROM model_usage WHERE state = 'UNKNOWN'")
    )


def _live_blocks(store, settings, now, *, cumulative_state=None, carry_view=None):
    approval = settings.live_text_window
    reasons = []
    if owner_authority.required(store, settings):
        reasons += _owner_blocks(store, settings, now)
    elif live_text.active(store, settings):
        if approval is None:
            reasons.append("LIVE_APPROVAL_REQUIRED")
        else:
            if not approval.live_approved:
                reasons.append("LIVE_APPROVAL_REQUIRED")
            if not approval.starts_at <= datetime.fromisoformat(now) < approval.ends_at:
                reasons.append("LIVE_APPROVAL_EXPIRED")
        if live_text.stopped(store, approval.plan_sha256 if approval else None):
            reasons.append("LIVE_WINDOW_STOPPED")
    if store.one(
        "SELECT 1 FROM runtime_meta WHERE key IN ('model_budget_blocked', "
        "'model_budget_restore_required')"
    ):
        reasons.append("BUDGET_RECONCILIATION_REQUIRED")
    carry_view = _carry_view(store, settings) if carry_view is None else carry_view
    closed = sorted(carry_view.get("model_closures", {}))
    if not model_carry.active(settings) and store.one(
        "SELECT 1 FROM model_usage WHERE state='UNKNOWN'"
        + (" AND request_id NOT IN (" + ",".join('%s' for _ in closed) + ")" if closed else "")
        + " LIMIT 1",
        closed,
    ):
        reasons.append("MODEL_USAGE_UNKNOWN")
    if store.one(
        "SELECT 1 FROM model_usage AS m LEFT JOIN model_admissions AS a USING "
        "(request_id) WHERE m.state = 'RESERVED' AND (a.request_id IS NULL OR a.lease_until <= %s) "
        "LIMIT 1",
        (now,),
    ):
        reasons.append("MODEL_USAGE_UNRECONCILED")
    if store.one("SELECT 1 FROM outbox WHERE state = 'DELIVERY_UNCERTAIN' LIMIT 1"):
        reasons.append("DELIVERY_RECONCILIATION_REQUIRED")
    return reasons + _cumulative_blocks(store, settings, now, state=cumulative_state)


def _owner_blocks(store, settings, now, purpose=None):
    """Project current native admission without creating work or a source lease."""
    try:
        owner_authority.current(store, settings, lambda: now, purpose=purpose)
    except CoreError as error:
        return [error.code]
    return []


def _model_blocks(
    store, settings, now, *, live_blocks=None, cumulative_state=None, carry_view=None
):
    carry_view = _carry_view(store, settings) if carry_view is None else carry_view
    reasons = (
        list(live_blocks)
        if live_blocks is not None
        else _live_blocks(store, settings, now, carry_view=carry_view)
    )
    reasons += cloud_budget.capacity_blocks(store, settings, now)
    if settings.mode == "live":
        try:
            approval = paid_budget.contract(settings, now)
        except CoreError as error:
            reasons.append(error.code)
        else:
            reasons += _paid_capacity_blocks(store, settings, approval, now, carry_view=carry_view)
    else:
        reasons.append("DEMO_MODE")
    return reasons + _cumulative_blocks(
        store,
        settings,
        now,
        dimensions=("model_attempts", "input_tokens", "output_tokens", "micro_usd"),
        state=cumulative_state,
    )


def _cumulative_blocks(store, settings, now, *, dimensions=(), state=None):
    """The native status verifies its journal with initialization and repair disabled."""
    status = cumulative.status(store, settings, now) if state is None else state
    scope = settings.paid_model.cumulative if settings.paid_model else None
    if not status["enrolled"]:
        return ["CUMULATIVE_ACCOUNT_NOT_ENROLLED"] if scope else []
    if not status.get("available"):
        return ["BUDGET_RECONCILIATION_REQUIRED"]
    reasons = ["CUMULATIVE_PAUSED"] if status["paused"] else []
    if scope is None or status["policy_sha256"] != paid_budget.sha(scope.model_dump(mode="json")):
        reasons.append("CUMULATIVE_SCOPE_REQUIRED")
    if datetime.fromisoformat(status["expires_at"]) <= datetime.fromisoformat(now):
        reasons.append("CUMULATIVE_POLICY_EXPIRED")
    for dimension in dimensions:
        if any(
            status["used"][period][dimension] >= limits[dimension]
            for period, limits in status["limits"].items()
        ):
            reasons.append("CUMULATIVE_" + dimension.upper() + "_LIMIT")
    return reasons


def _private_scope(settings, field, expected, code, *, digest_field=None, digest=None):
    approval = settings.live_text_window
    if (
        settings.mode != "live"
        or not private_life.enabled(settings)
        or not approval
        or not approval.capture_public_key
        or getattr(approval, field, None) != expected
        or digest_field
        and getattr(approval, digest_field, None) != digest
    ):
        return [code]
    return []


def _scenario_blocks(store, settings, now, scenarios, amount, *, calls=None, unlimited=False):
    day, _ = _periods(settings, now)
    usage = store.one(
        (
            "SELECT COUNT(*) calls,COALESCE(SUM(billed_micro_usd),0)"
            " amount FROM model_usage WHERE occurred_at>=%s AND "
            "scenario IN ("
        )
        + ",".join('%s' for _ in scenarios)
        + ")",
        (day, *scenarios),
    )
    reasons = []
    if calls is not None and usage["calls"] >= calls:
        reasons.append("SCENARIO_CALL_LIMIT")
    if not unlimited and usage["amount"] >= amount:
        reasons.append("SCENARIO_BUDGET_DISABLED" if amount <= 0 else "DAILY_BUDGET_EXHAUSTED")
    return reasons


def _periods(settings, now):
    local = datetime.fromisoformat(now).astimezone(ZoneInfo(settings.timezone))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return tuple(
        at.astimezone(UTC).isoformat(timespec="microseconds") for at in (day, day.replace(day=1))
    )


def _paid_capacity_blocks(store, settings, approval, now, *, carry_view=None):
    """Report exhausted native quotas without estimating or reserving another call."""
    reasons = []
    if store.one(
        "SELECT 1 FROM model_usage AS m LEFT JOIN model_admissions AS a USING (request_id) WHERE "
        "a.request_id IS NULL AND m.state IN ('RESERVED', 'UNKNOWN') LIMIT 1",
    ):
        reasons.append("MODEL_LEGACY_USAGE_RECONCILIATION_REQUIRED")
    day, month = _periods(settings, now)
    daily = store.one(
        (
            "SELECT COUNT(*) calls,COALESCE(SUM(billed_micro_usd),0)"
            " amount FROM model_usage WHERE occurred_at>=%s"
        ),
        (day,),
    )
    unlimited = approval.unlimited_period_spend
    if daily["calls"] >= approval.daily_calls or (
        not unlimited and daily["amount"] >= approval.daily_micro_usd
    ):
        reasons.append("MODEL_DAILY_BUDGET_EXHAUSTED")
    if not unlimited:
        monthly = total_monthly(store, month)
        if settings.pricing_known and monthly >= settings.monthly_limit_micro_usd:
            reasons.append("MONTHLY_BUDGET_EXHAUSTED")
        if monthly >= approval.monthly_micro_usd:
            reasons.append("MODEL_MONTHLY_BUDGET_EXHAUSTED")
    carry_view = _carry_view(store, settings) if carry_view is None else carry_view
    windows = (
        model_carry.scope_windows(carry_view, approval.window_id)
        if carry_view
        else [approval.window_id]
    )
    window = store.one(
        "SELECT COUNT(*) calls,COALESCE(SUM(m.billed_micro_usd),0) amount "
        "FROM model_admissions a JOIN model_usage m USING(request_id) WHERE a.window_id IN ("
        + ",".join('%s' for _ in windows)
        + ")",
        windows,
    )
    if window["calls"] >= approval.window_calls or (
        not unlimited and window["amount"] >= approval.window_micro_usd
    ):
        reasons.append("MODEL_WINDOW_BUDGET_EXHAUSTED")
    if capacity_exhausted(store, approval, carry_view.get("model_closures", ())):
        reasons.append("MODEL_PROVIDER_CONCURRENCY_EXHAUSTED")
    return reasons


def _reflection_blocks(store, settings, now):
    day, month = _periods(settings, now)
    used = store.one(
        (
            "SELECT COUNT(CASE WHEN occurred_at>=%s THEN 1 END) "
            "calls,COALESCE(SUM(CASE WHEN occurred_at>=%s THEN "
            "billed_micro_usd ELSE 0 END),0) "
            "daily,COALESCE(SUM(billed_micro_usd),0) monthly FROM "
            "model_usage WHERE scenario='reflection' AND "
            "occurred_at>=%s"
        ),
        (day, day, month),
    )
    reasons = []
    if used["calls"] >= settings.reflection_max_daily_calls:
        reasons.append("SCENARIO_CALL_LIMIT")
    unlimited = bool(
        settings.mode == "live"
        and settings.paid_model
        and settings.paid_model.unlimited_period_spend
    )
    if not unlimited:
        if (
            settings.reflection_daily_micro_usd > 0
            and used["daily"] >= settings.reflection_daily_micro_usd
        ):
            reasons.append("DAILY_BUDGET_EXHAUSTED")
        if (
            settings.reflection_monthly_micro_usd > 0
            and used["monthly"] >= settings.reflection_monthly_micro_usd
        ):
            reasons.append("SCENARIO_MONTHLY_BUDGET_EXHAUSTED")
    return reasons


def public_source_readiness(
    store, settings, clock, adapter_id, *, integration=None, operation="read", live_blocks=None
):
    """Use native adapter/grant and reservation accounting without exposing custody."""
    now = clock()
    policy = settings.observation_adapters.get(adapter_id)
    observation, facts = _observation(integration, now)
    unavailable = []
    paused = list(live_blocks) if live_blocks is not None else _live_blocks(store, settings, now)
    if owner_authority.required(store, settings):
        unavailable += _owner_blocks(
            store, settings, now, "exploration" if operation == "discover" else "content"
        )
    limits = {}
    source = policy.source if policy else None
    if not policy or not supports(source, "get"):
        unavailable.append("READER_NOT_CONFIGURED")
    else:
        if operation not in {"read", "discover"} or not supports(source, operation):
            unavailable.append("READER_OPERATION_UNAVAILABLE")
        limits = {
            "max_requests": policy.reader_max_requests,
            "max_response_bytes": settings.exploration_max_response_bytes,
            "daily_requests": settings.exploration_daily_requests,
            "monthly_requests": settings.exploration_monthly_requests,
            "daily_bytes": settings.exploration_daily_bytes,
            "daily_deep_reads": settings.exploration_daily_deep_reads,
        }
        if not settings.observation_enabled or not (
            settings.content_enabled or settings.exploration_enabled
        ):
            unavailable.append("READING_DISABLED")
        if source == "web" and (not settings.web_reading_enabled or not settings.web_reading_hosts):
            unavailable.append("WEB_HOST_SCOPE_REQUIRED")
        if policy.source_grant_required:
            grants = store.rows(
                "SELECT r.grant_id, r.epoch, r.fingerprint, r.expires_at FROM source_grants AS g "
                "JOIN source_grant_revisions AS r USING (grant_id) WHERE g.adapter_id = %s AND "
                "r.epoch = (SELECT MAX(v.epoch) FROM source_grant_revisions AS v WHERE v.grant_id "
                "= g.grant_id)",
                (adapter_id,),
            )
            valid, grant_reasons = False, []
            for grant in grants:
                try:
                    SourceGrantService(store, settings, clock).require(
                        adapter_id,
                        SourceGrantSnapshot.model_validate(grant),
                    )
                    valid = True
                    break
                except CoreError as error:
                    grant_reasons.append(error.code)
            if not valid:
                unavailable.extend(grant_reasons or ["SOURCE_GRANT_REQUIRED"])
        day, month = _periods(settings, now)
        used = store.one(
            "SELECT SUM(accounted_requests) monthly, SUM(CASE WHEN started_at>=%s "
            "THEN accounted_requests ELSE 0 END) daily, SUM(CASE WHEN started_at>=%s "
            "THEN accounted_bytes ELSE 0 END) bytes, SUM(CASE WHEN started_at>=%s "
            "AND operation='read' THEN 1 ELSE 0 END) reads "
            f"FROM ({METERED_USAGE_SQL}) WHERE started_at>=%s",
            (day, day, day, month),
        )
        if (
            (used["daily"] or 0) + policy.reader_max_requests > limits["daily_requests"]
            or (used["monthly"] or 0) + policy.reader_max_requests > limits["monthly_requests"]
            or (used["bytes"] or 0) + limits["max_response_bytes"] > limits["daily_bytes"]
            or (operation == "read" and (used["reads"] or 0) >= limits["daily_deep_reads"])
        ):
            paused.append("READ_BUDGET_UNAVAILABLE")
    if observation == "STALE":
        paused.append("READER_OBSERVATION_STALE")
    elif facts.get("accepting_tasks") is False:
        paused.append("READER_NOT_ACCEPTING_TASKS")
    return {
        "adapter_id": adapter_id,
        "source": source,
        "state": "UNAVAILABLE" if unavailable else "PAUSED" if paused else "READY",
        "reason_codes": list(dict.fromkeys(unavailable + paused)),
        "limits": limits,
        "runtime_observation": observation,
    }


def _file_facts(store, settings, now, live_blocks):
    from companion import qq_files
    from companion.file_evidence import PrivateFilesConfig

    try:
        config = PrivateFilesConfig.model_validate(settings.private_files.model_dump())
    except ValueError:
        return {"unavailable": ["PRIVATE_FILE_CONFIG_INVALID"]}
    native = owner_authority.required(store, settings)
    unavailable = (
        _owner_blocks(store, settings, now, "file")
        if native
        else _private_scope(settings, "file_scope", "owner_file_v1", "PRIVATE_FILE_SCOPE_REQUIRED")
    )
    if native:
        if not settings.content_enabled or not settings.observation_enabled:
            unavailable.append("PRIVATE_FILE_SOURCE_REQUIRED")
    elif not unavailable:
        try:
            qq_files.policy(settings, settings.live_text_window)
        except CoreError as error:
            unavailable.append(error.code)
    adapter = settings.observation_adapters.get(config.adapter_id)
    if not native and (not adapter or adapter.source != "qq_file"):
        unavailable.append("PRIVATE_FILE_ADAPTER_UNAVAILABLE")
    if not config.parser_image:
        unavailable.append("PINNED_PARSER_IMAGE_REQUIRED")
    day, _ = _periods(settings, now)
    used = store.one(
        (
            "SELECT COUNT(*) files,COALESCE(SUM(reserved_bytes),0) "
            "bytes FROM content_items WHERE kind='qq_file' AND "
            "COALESCE(started_at,created_at)>=%s AND "
            "reserved_bytes>0"
        ),
        (day,),
    )
    paused = []
    if (
        used["files"] >= config.max_daily_files
        or used["bytes"] + config.max_file_bytes > config.max_daily_bytes
    ):
        paused.append("PRIVATE_FILE_TRAFFIC_EXHAUSTED")
    return {
        "enabled": config.enabled,
        "unavailable": unavailable,
        "paused": live_blocks + paused,
        "limits": {
            "max_file_bytes": config.max_file_bytes,
            "max_files_per_event": config.max_files_per_event,
            "daily_files": config.max_daily_files,
            "daily_bytes": config.max_daily_bytes,
            "max_text_chars": config.limits.max_text_chars,
            "parser_transport": ("host_socket" if config.parser_socket else "isolated_container"),
        },
    }


def _bili_video_facts(store, settings, now, readers, model_blocks):
    from companion.bili_video_protocol import CAPABILITY, MAX_METADATA_BYTES

    config, approval = settings.bili_video, settings.paid_model
    native = owner_authority.required(store, settings)
    unavailable = (
        _owner_blocks(store, settings, now, "bili_video")
        + _owner_blocks(store, settings, now, "content")
        if native
        else _private_scope(
            settings,
            "bili_video_scope",
            CAPABILITY,
            "BILI_VIDEO_SCOPE_REQUIRED",
            digest_field="bili_video_policy_sha256",
            digest=paid_budget.sha(config.model_dump(mode="json")),
        )
    )
    if not settings.content_enabled:
        unavailable.append("CONTENT_DISABLED")
    if not native and (
        not settings.live_text_window or settings.live_text_window.link_scope != "owner_links_v1"
    ):
        unavailable.append("LIVE_LINK_SCOPE_REQUIRED")
    try:
        video_input.contract(settings, "capability-check", "capability-check")
    except CoreError as error:
        unavailable.append(error.code)
    if not approval or not approval.image_input:
        unavailable.append("PAID_IMAGE_APPROVAL_REQUIRED")
    choices = [
        row
        for row in readers
        if row["source"] == "bilibili"
        and settings.observation_adapters[row["adapter_id"]].media_analysis
    ]
    if not choices or all(row["state"] == "UNAVAILABLE" for row in choices):
        unavailable.append("BILI_VIDEO_READER_UNAVAILABLE")
    paused = []
    if choices and not any(row["state"] == "READY" for row in choices):
        paused.append("BILI_VIDEO_READER_PAUSED")
    if store.one("SELECT 1 FROM runtime_meta WHERE key = 'bili_video_restore_required'"):
        paused.append("BILI_VIDEO_RECONCILIATION_REQUIRED")
    active = store.one(
        "SELECT COUNT(*) AS n FROM bili_video_items AS b JOIN exploration_requests "
        "AS r USING (request_id) WHERE r.state = 'RUNNING'"
    )["n"]
    if active >= config.max_concurrent:
        paused.append("BILI_VIDEO_CONCURRENCY_LIMIT")
    day, month = _periods(settings, now)
    used = store.one(
        "SELECT SUM(accounted_requests) monthly,SUM(CASE WHEN started_at>=%s "
        "THEN accounted_requests ELSE 0 END) daily,SUM(CASE WHEN started_at>=%s "
        "THEN accounted_bytes ELSE 0 END) bytes,SUM(CASE WHEN started_at>=%s "
        "AND operation='read' THEN 1 ELSE 0 END) reads "
        f"FROM ({METERED_USAGE_SQL}) WHERE started_at>=%s",
        (day, day, day, month),
    )
    media_bytes = config.media.max_input_bytes + MAX_METADATA_BYTES
    if (
        (used["daily"] or 0) + 8 > settings.exploration_daily_requests
        or (used["monthly"] or 0) + 8 > settings.exploration_monthly_requests
        or (used["bytes"] or 0) + media_bytes > settings.exploration_daily_bytes
        or (used["reads"] or 0) >= settings.exploration_daily_deep_reads
    ):
        paused.append("READ_BUDGET_UNAVAILABLE")
    paused += _scenario_blocks(
        store,
        settings,
        now,
        ("content_vision", "content_video_preview", "content_video"),
        settings.content.image_daily_micro_usd,
        unlimited=bool(approval and approval.unlimited_period_spend),
    )
    observation = (
        "OBSERVED"
        if any(row["runtime_observation"] == "OBSERVED" for row in choices)
        else "STALE"
        if choices and all(row["runtime_observation"] == "STALE" for row in choices)
        else "UNOBSERVED"
    )
    return {
        "enabled": config.enabled,
        "unavailable": unavailable,
        "paused": model_blocks + paused,
        "observation": observation,
        "limits": {
            "max_source_seconds": config.media.max_source_seconds,
            "preview_frames": config.sampling.initial_frames,
            "max_clips": config.sampling.max_segments,
            "max_total_clip_seconds": config.sampling.max_total_segment_seconds,
            "max_read_requests": 8,
            "max_read_bytes": media_bytes,
        },
    }


def _voice_facts(store, settings, now, operation, live_blocks):
    config = settings.voice
    try:
        policy_hash = config.authority_sha256() if config.enabled else None
    except ValueError:
        return {"unavailable": ["VOICE_CONFIG_INVALID"]}
    native = owner_authority.required(store, settings)
    if native:
        from companion import voice_authority

        unavailable = []
        try:
            voice_authority.current(store, settings, lambda: now)
        except CoreError as error:
            unavailable.append(error.code)
    else:
        unavailable = _private_scope(
            settings,
            "voice_scope",
            "owner_voice_v1",
            "VOICE_SCOPE_REQUIRED",
            digest_field="voice_policy_sha256",
            digest=policy_hash,
        )
    nodes = {
        key: node
        for key, node in config.nodes.items()
        if node.enabled and operation in node.capabilities
    }
    if not nodes:
        unavailable.append("VOICE_NODE_SCOPE_REQUIRED")
    if (
        not native
        and operation == "asr"
        and (
            not settings.live_text_window
            or settings.live_text_window.rich_input_scope != "references_v1"
        )
    ):
        unavailable.append("VOICE_ASR_SCOPE_REQUIRED")
    policy = {"asr": config.asr, "tts": config.reply, "sing": config.singing}[operation]
    used = store.one(

            "SELECT COUNT(CASE WHEN state IN ('QUEUED','LEASED',"
            "'RUNNING') THEN 1 END) pending,"
            'COALESCE(SUM(COALESCE(octet_length(input_blob),'
            '0)+GREATEST(COALESCE(octet_length(result_blob),0),'
            'reserved_output_bytes)),0) bytes FROM voice_jobs'

    )
    paused = []
    if used["pending"] >= config.max_pending_jobs or used["bytes"] >= config.max_private_blob_bytes:
        paused.append("VOICE_CAPACITY_EXCEEDED")
    states = [
        row
        for row in store.rows(
            "SELECT v.node_id AS node_id, v.active_job_id AS "
            "active_job_id, v.resource_state AS resource_state, v.last_seen_at AS last_seen_at, "
            "v.last_error_code AS last_error_code, v.last_claim_seq AS last_claim_seq, "
            "v.claim_request_sha256 AS claim_request_sha256, v.claim_response_json AS "
            "claim_response_json, v.claim_expires_at AS claim_expires_at, v.claim_job_id AS "
            "claim_job_id, v.recovery_required AS recovery_required, v.recovered_incarnation AS "
            "recovered_incarnation, j.state AS job_state FROM voice_node_state AS v LEFT JOIN "
            "voice_jobs AS j ON v.active_job_id = j.job_id"
        )
        if row["node_id"] in nodes
    ]
    unavailable_nodes = {
        row["node_id"]
        for row in states
        if row["resource_state"] == "QUARANTINED" or row["job_state"] == "UNKNOWN"
    }
    if nodes and set(nodes) <= unavailable_nodes:
        paused.append("VOICE_RESOURCE_RECONCILIATION_REQUIRED")
    age = []
    for row in states:
        try:
            elapsed = (
                datetime.fromisoformat(now) - datetime.fromisoformat(row["last_seen_at"])
            ).total_seconds()
            if elapsed >= 0:
                age.append(elapsed)
        except (TypeError, ValueError):
            continue
    observation = (
        "OBSERVED"
        if any(value <= config.lease_ttl_seconds for value in age)
        else "STALE"
        if age
        else "UNOBSERVED"
    )
    limits = {
        "max_pending_jobs": config.max_pending_jobs,
        "max_private_blob_bytes": config.max_private_blob_bytes,
    }
    if operation == "sing":
        if policy is not None:
            limits.update(
                max_seconds=policy.max_seconds,
                max_input_bytes=policy.max_input_bytes,
                segment_seconds=55,
                max_segments=24,
            )
            try:
                from companion.singing import policy as singing_policy

                singing_policy(settings)
            except CoreError as error:
                unavailable.append(error.code)
    else:
        limits["wait_seconds"] = policy.wait_seconds
    if operation == "asr":
        limits.update(max_records=policy.max_records, max_context_chars=policy.max_context_chars)
    elif operation == "tts":
        limits.update(max_chars=policy.max_chars, mode=policy.mode)
    enabled = policy is not None if operation == "sing" else policy.enabled
    return {
        "enabled": config.enabled and enabled,
        "unavailable": unavailable,
        "paused": live_blocks + paused,
        "observation": observation,
        "limits": limits,
    }


def _poke_facts(store, settings, now, model_blocks, send_blocks, *, native=False):
    from companion import owner_authority, poke

    config = settings.poke_policy
    unavailable = []
    try:
        view = owner_authority.current(store, settings, lambda: now, purpose="poke")
        poke.policy(settings, view)
    except CoreError as error:
        unavailable.append(error.code)
    if native and (not config.allow_native or config.mode not in {"auto", "poke_only"}):
        unavailable.append("PRIVATE_POKE_NATIVE_DISABLED")
    paused = []
    stamps = store.rows("SELECT claimed_at FROM poke_batches WHERE NOT claimed_at IS NULL")
    day, _ = _periods(settings, now)
    if (
        config.mode == "none"
        or sum(row["claimed_at"] >= day for row in stamps) >= config.daily_limit
    ):
        paused.append("POKE_RATE_LIMIT")
    current = datetime.fromisoformat(now)
    if any(
        (current - datetime.fromisoformat(row["claimed_at"])).total_seconds()
        < config.cooldown_seconds
        for row in stamps
    ):
        paused.append("POKE_COOLDOWN")
    return {
        "enabled": config.enabled,
        "unavailable": unavailable,
        "paused": model_blocks + paused + send_blocks,
        "limits": {
            "daily_limit": config.daily_limit,
            "cooldown_seconds": config.cooldown_seconds,
            "mode": config.mode,
        },
    }


def _account_facts(store, settings, now, field, live_blocks, model_blocks, send_blocks):
    from companion.social_autonomy import SocialAutonomy

    module = SocialAutonomy(store, settings, clock=lambda: now)
    config = module.config
    unavailable = (
        _owner_blocks(store, settings, now, "social")
        if owner_authority.required(store, settings)
        else _private_scope(
            settings,
            "account_actions_scope",
            "qq_social_v1",
            "SOCIAL_LIVE_SCOPE_REQUIRED",
            digest_field="account_actions_authorization_sha256",
            digest=config.authorization_sha256,
        )
    )
    if field != "observe" and owner_authority.required(store, settings):
        unavailable += _owner_blocks(store, settings, now, "account_actions")
    paused = []
    observation = "UNOBSERVED"
    try:
        module._policy()
        if field != "observe":
            module._planning_room(config, now, settings.timezone)
            module._capacity(
                "qzone.publish" if field == "qzone" else "qq.profile." + field,
                settings.timezone,
                now,
            )
            if field != "qzone":
                module._profile(field, now)
                observation = "OBSERVED"
            if field == "avatar":
                assets = store.rows(
                    "SELECT asset_id FROM social_autonomy_assets WHERE revoked_at IS NULL"
                )
                valid = False
                for asset in assets:
                    try:
                        module._asset(asset["asset_id"], now)
                        valid = True
                        break
                    except CoreError:
                        continue
                if not valid:
                    paused.append("SOCIAL_REVIEWED_AVATAR_REQUIRED")
    except CoreError as error:
        if error.code in {"SOCIAL_AUTONOMY_DISABLED", "SOCIAL_LIVE_SCOPE_REQUIRED"}:
            unavailable.append(error.code)
        else:
            paused.append(error.code)
    if field != "observe":
        paused += model_blocks
        paused += _scenario_blocks(
            store,
            settings,
            now,
            ("social_autonomy",),
            config.model_daily_micro_usd,
            calls=config.model_daily_calls,
        )
        paused += send_blocks
    else:
        paused += live_blocks
    limits = {"max_observation_age_seconds": config.profile_baseline_seconds}
    if field != "observe":
        limits = {
            "daily_limit": config.qzone_daily_limit
            if field == "qzone"
            else config.profile_daily_limit,
            "model_daily_calls": config.model_daily_calls,
            "cooldown_seconds": config.qzone_cooldown_seconds
            if field == "qzone"
            else config.profile_cooldown_seconds,
        }
    return {
        "enabled": config.enabled and (field == "observe" or getattr(config, field + "_enabled")),
        "unavailable": unavailable,
        "paused": paused,
        "observation": observation,
        "limits": limits,
    }


def snapshot(store, settings, clock, *, model_tools=(), integrations=None):
    """Return a closed, credential-free projection; integration data is never echoed."""
    now, cfg = clock(), settings
    integrations = integrations if isinstance(integrations, dict) else {}
    readers = integrations.get("readers", {})
    readers = readers if isinstance(readers, dict) else {}
    rows = []
    quota = cumulative.status(store, cfg, now)
    carry_view = _carry_view(store, cfg)
    live_blocks = _live_blocks(store, cfg, now, cumulative_state=quota, carry_view=carry_view)
    model_blocks = _model_blocks(
        store, cfg, now, live_blocks=live_blocks, cumulative_state=quota, carry_view=carry_view
    )
    send_blocks = _cumulative_blocks(store, cfg, now, dimensions=("send_attempts",), state=quota)
    life_pause = ["LIFE_PAUSED"] if _meta(store, "life_paused") == "1" else []
    native = owner_authority.required(store, cfg)
    purpose_by_feature = {
        "memory.manage": "memory",
        "memory.recall": "memory",
        "diary": "diary",
        "consolidation": "consolidation",
        "reflection": "reflection",
        "wake": "wake",
        "affect": "affect",
        "expression": "expression",
        "expression_learning": "expression",
        "goals": "goals",
        "proactive": "proactive",
        "social": "social",
        "exploration": "exploration",
        "tool.capability_status": "chat",
        "tool.current_time": "chat",
        "tool.diary_status": "diary",
        "tool.diary_list": "diary",
        "tool.diary_read": "diary",
        "tool.memory_recall": "memory",
        "tool.memory_history": "memory",
        "tool.goal_list": "goals",
        "tool.reading_catalog": "content",
        "tool.read_content": "content",
        "tool.discover_content": "exploration",
        "tool.reading_status": "content",
        "tool.cancel_reading": "content",
        "model.understand_video": "image",
        "tool.analyze_shared_video": "bili_video",
        "tool.search_meme_wiki": "chat",
    }

    def add(
        identifier,
        summary,
        invocation=("automatic",),
        *,
        enabled=True,
        unavailable=(),
        paused=(),
        limits=None,
        observation="UNOBSERVED",
    ):
        unavailable = list(unavailable) + ([] if enabled else ["FEATURE_DISABLED"])
        if native and identifier in purpose_by_feature:
            unavailable += _owner_blocks(store, cfg, now, purpose_by_feature[identifier])
        if native and identifier == "tool.analyze_shared_video":
            unavailable += _owner_blocks(store, cfg, now, "content")
        custody_blocks = [
            code for code in unavailable if code == "MODEL_BUDGET_RECONCILIATION_REQUIRED"
        ]
        unavailable = [code for code in unavailable if code not in custody_blocks]
        paused = [*paused, *custody_blocks]
        rows.append(
            {
                "id": identifier,
                "state": "UNAVAILABLE" if unavailable else "PAUSED" if paused else "READY",
                "invocation": list(invocation),
                "summary": summary,
                "reason_codes": list(dict.fromkeys([*unavailable, *paused])),
                "limits": limits or {},
                "runtime_observation": observation,
            }
        )

    add(
        "memory.manage",
        "主人可查询、确认、纠正和忘记记忆。",
        ("owner_command",),
        enabled=cfg.memory_enabled,
        unavailable=["OWNER_COMMAND_SCOPE_REQUIRED"]
        if not native and private_life.strict(store, cfg)
        else [],
    )
    add(
        "memory.recall",
        "对相关记忆做有来源的检索与审核。",
        enabled=cfg.memory_enabled,
        paused=model_blocks
        + ([] if cfg.memory_daily_micro_usd > 0 else ["SCENARIO_BUDGET_DISABLED"]),
        limits={"daily_micro_usd": cfg.memory_daily_micro_usd},
    )
    for name, summary, extra, limits in (
        (
            "diary",
            "按已有对话来源整理日记。",
            (["DIARY_PAUSED"] if _meta(store, "diary_paused") == "1" else [])
            + (["COMPANION_PAUSED"] if delegation.paused(store, "diary", now) else []),
            {"daily_micro_usd": cfg.diary_daily_micro_usd},
        ),
        ("consolidation", "整理已有记忆和日常记录。", life_pause, {}),
        (
            "reflection",
            "基于已有来源提出反思建议。",
            life_pause,
            {
                "daily_micro_usd": cfg.reflection_daily_micro_usd,
                "monthly_micro_usd": cfg.reflection_monthly_micro_usd,
                "max_daily_calls": cfg.reflection_max_daily_calls,
            },
        ),
        (
            "wake",
            "在已有目标和限额内调度自主活动。",
            life_pause + (["COMPANION_PAUSED"] if delegation.paused(store, "wake", now) else []),
            {
                "max_daily_runs": delegation.effective(
                    store, cfg, "wake", "wake_max_daily_runs", now
                )
            },
        ),
    ):
        paused = model_blocks + extra
        if limits and any(value <= 0 for value in limits.values()):
            paused += ["SCENARIO_BUDGET_DISABLED"]
        if name == "reflection":
            paused += _reflection_blocks(store, cfg, now)
        if name == "diary" and _meta(store, "owner_qq_id") != cfg.owner_qq_id:
            paused += ["OWNER_IDENTITY_REQUIRED"]
        if name == "wake" and not (cfg.goals_enabled and cfg.exploration_enabled):
            paused += ["GOALS_AND_EXPLORATION_REQUIRED"]
        add(name, summary, enabled=getattr(cfg, name + "_enabled"), paused=paused, limits=limits)
    for name, summary in (
        ("affect", "结合已有互动线索调整回应状态。"),
        ("expression", "在回复中使用已有表达偏好。"),
        ("expression_learning", "从已有互动中学习表达偏好。"),
    ):
        add(name, summary, enabled=getattr(cfg, name + "_enabled"), paused=live_blocks)
    add(
        "goals",
        "主人可查看、接受和取消目标及承诺。",
        ("owner_command",),
        enabled=cfg.goals_enabled,
        paused=live_blocks,
        limits={"max_active": cfg.goals_max_active},
    )
    from companion.social import proactive_allowed

    sharing = [] if proactive_allowed(store, cfg, now) else ["PROACTIVE_PAUSED_OR_QUIET"]
    for name, summary in (
        ("proactive", "在主动交流限额内发起私聊。"),
        ("social", "把已读取且适合分享的内容带入私聊。"),
    ):
        add(
            name,
            summary,
            enabled=getattr(cfg, name + "_enabled"),
            paused=model_blocks + sharing,
            limits={"daily_limit": cfg.proactive_daily_limit},
        )

    readable, discoverable = [], []
    for adapter_id, policy in sorted(cfg.observation_adapters.items()):
        if not supports(policy.source, "get"):
            continue
        ready = public_source_readiness(
            store,
            cfg,
            clock,
            adapter_id,
            integration=readers.get(adapter_id),
            live_blocks=live_blocks,
        )
        readable.append(ready)
        discoverable.append(
            public_source_readiness(
                store,
                cfg,
                clock,
                adapter_id,
                integration=readers.get(adapter_id),
                operation="discover",
                live_blocks=live_blocks,
            )
        )
        summaries = {
            "bilibili": "读取 B站元数据、可用字幕及有证据的部分评论和弹幕；覆盖以读取回执为准。",
            "heybox": "读取小黑盒文章正文、图片引用和有证据的部分评论；覆盖以读取回执为准。",
            "web": "读取已配置主机范围内的网页正文。",
        }
        rows.append(
            {
                "id": "reading." + adapter_id,
                "invocation": ["automatic", "owner_command"],
                "summary": summaries.get(policy.source, "读取已授权来源的内容。"),
                **{
                    key: value
                    for key, value in ready.items()
                    if key not in {"adapter_id", "source"}
                },
            }
        )
    for source, label in (("bilibili", "B站"), ("heybox", "小黑盒"), ("web", "其他网页")):
        if not any(row["source"] == source for row in readable):
            add(
                "reading." + source,
                "读取" + label + "内容。",
                unavailable=["READER_NOT_CONFIGURED"],
            )
    add(
        "exploration",
        "按已有兴趣和目标发现可读内容。",
        enabled=cfg.exploration_enabled,
        unavailable=[]
        if any(row["state"] != "UNAVAILABLE" for row in discoverable)
        else ["READER_UNAVAILABLE"],
        paused=live_blocks
        + life_pause
        + (
            []
            if any(row["state"] == "READY" for row in discoverable)
            else ["READ_BUDGET_UNAVAILABLE"]
        ),
    )
    add(
        "bilibili.video",
        "分析已读取B站视频的受限预览帧及按需短片段；"
        "结论仅覆盖回执列出的采样范围，字幕不代表看过完整视频。",
        **_bili_video_facts(store, cfg, now, readable, model_blocks),
    )
    add(
        "qq.files",
        "解析主人本次发送的DOCX、PDF、DOC、TXT和Markdown；"
        "只依据解析回执中的文本、表格与页码，扫描件或截断不能假称完整读取。",
        **_file_facts(store, cfg, now, live_blocks),
    )
    add(
        "qq.voice.asr",
        "接收主人当前语音并生成有范围标注的机器转写；转写可能出错，不能当作主人原话、命令或授权。",
        **_voice_facts(store, cfg, now, "asr", live_blocks),
    )
    add(
        "qq.voice.tts",
        "把当前已授权回复转为语音；等待和失败按策略回退文字，是否发出以原发送回执为准。",
        **_voice_facts(store, cfg, now, "tts", live_blocks),
    )
    add(
        "qq.voice.singing",
        "本轮私聊可按点歌或气氛选择真实 B 站歌曲，用固定本地音色翻唱；"
        "完成后按序发送完整语音气泡。UNKNOWN 停止后续派发。",
        **_voice_facts(store, cfg, now, "sing", live_blocks),
    )
    add(
        "qq.poke",
        "按策略回应主人私聊的原生戳一戳通知，可选择文字、回拍或不回应。",
        **_poke_facts(store, cfg, now, model_blocks, send_blocks),
    )
    add(
        "qq.poke.native",
        "在单独许可与互动限额内向主人回拍一次。",
        **_poke_facts(store, cfg, now, model_blocks, send_blocks, native=True),
    )
    for field, label in (
        ("qzone", "向主人可见的QQ空间发布有公开来源依据的内容"),
        ("nickname", "调整昵称"),
        ("signature", "调整个性签名"),
        ("avatar", "从已审核素材中选择头像"),
    ):
        add(
            "qq.account." + ("qzone_publish" if field == "qzone" else field),
            "在独立授权、原配额和冷却期内" + label + "；实际结果须有原生回执。",
            **_account_facts(store, cfg, now, field, live_blocks, model_blocks, send_blocks),
        )
    add(
        "qq.profile.observe",
        "读取账号资料的API观测可能来自缓存；观测时刻不证明网络刷新，也不证明资料修改成功。",
        **_account_facts(store, cfg, now, "observe", live_blocks, model_blocks, send_blocks),
    )

    image_errors = []
    try:
        if owner_authority.required(store, cfg):
            from companion import agent_images

            image_errors += _owner_blocks(store, cfg, now, "image")
            if not agent_images.enabled(cfg):
                image_errors.append("OWNER_IMAGE_SOURCE_CHANGED")
        else:
            if cfg.live_text_window is None:
                raise CoreError("LIVE_IMAGE_APPROVAL_REQUIRED", 403)
            qq_images.policy(cfg, cfg.live_text_window)
    except CoreError as error:
        image_errors.append(error.code)
    add(
        "qq.images",
        "分析本次主人发送且已认证捕获的图片。",
        unavailable=image_errors,
        paused=model_blocks,
    )
    video_errors = _owner_blocks(store, cfg, now, "content") if native else []
    if native and not cfg.content_enabled:
        video_errors.append("CONTENT_DISABLED")
    try:
        video_input.contract(cfg, "capability-check", "capability-check")
    except CoreError as error:
        video_errors.append(error.code)
    names = {value for value in model_tools if isinstance(value, str)}
    names.update(
        value.get("function", {}).get("name")
        for value in model_tools
        if isinstance(value, dict) and isinstance(value.get("function"), dict)
    )
    native_queries = {
        "capability_status": True,
        "current_time": True,
        "diary_status": True,
        "diary_list": cfg.diary_enabled,
        "diary_read": cfg.diary_enabled,
        "memory_recall": cfg.memory_enabled,
        "memory_history": cfg.memory_enabled,
        "goal_list": cfg.goals_enabled,
        "search_meme_wiki": cfg.meme_wiki.enabled,
        "reading_catalog": cfg.content_enabled and cfg.observation_enabled,
        "read_content": cfg.content_enabled and cfg.observation_enabled,
        "discover_content": cfg.exploration_enabled and cfg.observation_enabled,
        "reading_status": cfg.content_enabled and cfg.observation_enabled,
        "cancel_reading": cfg.content_enabled and cfg.observation_enabled,
        "analyze_shared_video": cfg.bili_video.enabled
        and cfg.content_enabled
        and cfg.observation_enabled,
    }
    for name in sorted(names & native_queries.keys()):
        add(
            "tool." + name,
            "已绑定本轮工具；具体结果以调用回执为准。",
            ("model_callable",),
            enabled=native_queries[name],
            paused=model_blocks,
            unavailable=[],
        )
    add(
        "qq.animation",
        "通过 understand_video 分析本次主人动图的一个完整动画周期。",
        unavailable=video_errors + image_errors,
        paused=model_blocks,
    )
    add(
        "model.understand_video",
        "仅分析当前已捕获的主人动图；只在本轮实际提供工具时可调用。",
        ("model_callable",),
        unavailable=video_errors
        + ([] if "understand_video" in names else ["TOOL_NOT_EXPOSED_THIS_TURN"]),
        paused=model_blocks,
    )
    add("unicode.emoji", "保留并理解消息中的 Unicode 表情原文。")
    add(
        "qq.face_names",
        "识别 QQ 原生表情 ID 的名称。",
        enabled=cfg.rich_input_scope == "references_v1"
        or bool(cfg.live_text_window and cfg.live_text_window.rich_input_scope == "references_v1"),
    )
    face_observation, faces = _observation(integrations.get("qq_faces"), now)
    face_paused = ["STATIC_FACE_IMAGES_PAUSED"] if faces.get("static_images") is False else []
    if face_observation == "STALE":
        face_paused.append("STATIC_FACE_OBSERVATION_STALE")
    add(
        "qq.face_images",
        "可接收认证捕获的 QQ 原生表情静态图；是否看过以本轮图像回执为准。",
        unavailable=image_errors,
        paused=model_blocks + face_paused,
        observation=face_observation,
    )

    bridge_observation, bridge = _observation(integrations.get("bridge_memes"), now)
    private_errors = (
        _owner_blocks(store, cfg, now, "meme")
        if native
        else []
        if private_life.enabled(cfg)
        else ["MEME_DISCOVERY_PRIVATE_SCOPE_REQUIRED"]
    )
    for name, summary, flag, connected in (
        (
            "catalog",
            "系统可接收 Bridge 已审核候选；实际素材以 Bridge 回执为准。",
            cfg.expression_enabled,
            bridge.get("catalog_ready"),
        ),
        (
            "community_import",
            "系统可安排受限发现导入；实际来源配置和素材以 Bridge 回执为准。",
            cfg.meme_discovery_enabled,
            bridge.get("community_import"),
        ),
    ):
        paused = ["BRIDGE_CAPABILITY_PAUSED"] if connected is False else []
        if bridge_observation == "STALE":
            paused.append("BRIDGE_OBSERVATION_STALE")
        add(
            "memes." + name,
            summary,
            enabled=flag,
            unavailable=private_errors,
            paused=model_blocks + paused,
            observation=bridge_observation,
        )
    add(
        "memes.search",
        "通过已配置的搜索服务发现表情资源。",
        enabled=cfg.meme_discovery_enabled,
        unavailable=private_errors
        + (
            []
            if bridge.get("search_provider") == "brave" and bridge.get("search_ready") is True
            else ["SEARCH_PROVIDER_UNOBSERVED"]
        ),
        paused=model_blocks,
        observation=bridge_observation,
    )
    add(
        "memes.semantic",
        "对已取得来源证明的表情样本做语义理解。",
        enabled=cfg.meme_semantics_enabled,
        unavailable=private_errors
        + ([] if cfg.paid_model and cfg.paid_model.meme_input else ["PAID_MEME_APPROVAL_REQUIRED"]),
        paused=model_blocks
        + ([] if cfg.meme_caption_daily_micro_usd > 0 else ["SCENARIO_BUDGET_DISABLED"]),
    )
    for name in ("voice", "camera", "android"):
        adapter = cfg.observation_adapters.get(getattr(cfg.media, name + "_adapter"))
        add(
            "media." + name,
            "主人可显式开启受限的"
            + {"voice": "语音", "camera": "摄像头", "android": "Android 观察"}[name]
            + "会话。",
            ("owner_command",),
            enabled=cfg.media.enabled and getattr(cfg, name + "_enabled"),
            unavailable=[]
            if cfg.observation_enabled and adapter and adapter.source == name
            else ["MEDIA_ADAPTER_NOT_CONFIGURED"],
            paused=live_blocks,
        )
    add(
        "executive",
        "主人可提交受独立审批约束的执行任务。",
        ("owner_command",),
        enabled=cfg.executive.enabled,
        unavailable=[] if cfg.executive.endpoints else ["EXECUTOR_NOT_CONFIGURED"],
        paused=live_blocks,
    )
    return {"schema_version": 1, "capabilities": rows}


def prompt(summary, *, compact=False):
    return "".join(prompt_parts(summary, compact=compact))


def prompt_parts(summary, *, compact=False):
    """Render only the safe capability projection already used by the console."""
    labels = {"READY": "可用", "PAUSED": "暂缓", "UNAVAILABLE": "不可用"}
    routes = {"automatic": "系统流程", "owner_command": "主人指令", "model_callable": "本轮工具"}
    answer_rules = (
        "回答能力问题时，以本轮状态为准；历史中的能力自述可能已过时。"
        "可用的系统流程和主人指令也是已接入的能力；本轮没有可直接调用的函数，"
        "不表示这些能力未接入。没有本次回执只表示尚未确认执行结果；"
        "暂缓或不可用只描述当前接纳状态，不能推断以前未执行；历史操作以原生回执为准。"
        "单次读取失败只说明该次失败，不能据此否定整个读取通道。"
    )
    lines = [
        "\n当前能力：可用表示这边能接这类事，不代表已经做了；被问能做什么时用自己的话说。",
        answer_rules,
        "系统流程和主人指令不等于模型工具。只有本轮可调用工具能直接调用。",
        "没有回执不要声称看过、听过、试过或完成；B站字幕不代表看过完整视频。",
    ]
    tools = [
        row["id"]
        for row in summary["capabilities"]
        if row["state"] == "READY" and "model_callable" in row["invocation"]
    ]
    dynamic = ["本轮可调用工具：" + ("、".join(tools) if tools else "无") + "。"]
    if compact:
        # Keep the full state inventory without displacing current reading evidence.
        lines = [
            "\n能力状态是本地接纳条件，不是已执行或远端在线；"
            "进程观测与本次任务回执各自独立，以本轮回执确认结果。",
            answer_rules,
            "系统流程和主人指令不等于模型工具；没有回执不要说已经试过或做过。",
            "B站字幕不代表看过完整视频；画面结论仅覆盖回执中的采样帧/短片段。"
            "文档以解析文本/表格/页码为准，语音转写不等于主人原话或授权。"
            "小黑盒正文/图片引用/部分评论、图片/动图/表情以回执为准。"
            "账号资料API观测可能来自缓存，观测时刻不证明网络刷新。",
        ]
        dynamic = ["本轮工具：" + ("、".join(tools) if tools else "无") + "。"]
        for state, label in labels.items():
            names = [row["id"] for row in summary["capabilities"] if row["state"] == state]
            if names:
                selected = []
                omitted = "部分能力未列出；未列出的能力不推定可用。"
                for name in names:
                    line = label + "：" + ",".join([*selected, name]) + "。"
                    if len("\n".join([*lines, *dynamic, line, omitted]).encode()) > 3000:
                        if selected:
                            dynamic.append(label + "：" + ",".join(selected) + "。")
                        return "\n".join(lines), "\n" + "\n".join([*dynamic, omitted])
                    selected.append(name)
                dynamic.append(label + "：" + ",".join(selected) + "。")
        return "\n".join(lines), "\n" + "\n".join(dynamic)
    omitted = "部分能力未列出；未列出的能力不推定可用。"
    for row in summary["capabilities"]:
        if row["state"] == "READY" and row["invocation"] == ["model_callable"]:
            continue  # Already named on the callable-tools line.
        line = f"- {row['id']}：{labels[row['state']]}"
        if row["state"] == "READY":
            line += f"（{'/'.join(routes[value] for value in row['invocation'])}）{row['summary']}"
        if row["reason_codes"]:
            line += "（" + row["reason_codes"][0] + "）"
        if len(("\n".join([*lines, *dynamic, line, omitted])).encode("utf-8")) > 6000:
            dynamic.append(omitted)
            break
        dynamic.append(line)
    return "\n".join(lines), "\n" + "\n".join(dynamic)
