import json
import re
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from companion import cloud_budget, cumulative, model_scope, paid_budget
from companion.contracts import CoreError
from companion.store import after, encode, uid
from companion.token_measurement import byte_estimate, measure_request
from companion.video_input import SCENARIOS as VIDEO_SCENARIOS
from companion.video_input import approved_route as video_route


def token_upper_bound(messages):
    # Conservative byte bound, not a tokenizer measurement; actual usage replaces it.
    return byte_estimate(messages)


def cost(settings, input_tokens, output_tokens, cached=0, *, scenario=None):
    video = video_route(settings, scenario)
    if video is None and not settings.pricing_known:
        return 0
    rates = video or settings
    numerator = (
        (input_tokens - cached) * rates.input_rate
        + cached * rates.cached_input_rate
        + output_tokens * rates.output_rate
    )
    return (numerator + 999999) // 1000000


def next_local_midnight(settings, now):
    local = datetime.fromisoformat(now).astimezone(ZoneInfo(settings.timezone))
    start = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).isoformat(timespec="microseconds")


def total_monthly(store, month, before=None):
    """Shared cost ceiling includes unknown/reserved model and isolated tool charges."""
    total = store.one(
        "SELECT COALESCE(SUM(billed_micro_usd),0) AS n FROM model_usage WHERE occurred_at>=%s"
        + (" AND occurred_at<%s" if before else ""),
        (month, before) if before else (month,),
    )["n"]
    if store.has_table("tool_tasks"):
        total += store.one(
            "SELECT COALESCE(SUM(billed_micro_usd),0) AS n FROM tool_tasks WHERE dispatched_at>=%s"
            + (" AND dispatched_at<%s" if before else ""),
            (month, before) if before else (month,),
        )["n"]
    return total


def chat_pending(store, now):
    return bool(
        store.one(
            (
                'SELECT 1 FROM jobs AS j JOIN events AS e ON e.event_id '
                "= j.event_id WHERE j.kind = 'chat' AND e.redacted = 0 "
                "AND j.state IN ('QUEUED', 'RUNNING', 'RETRY_WAIT') "
                'LIMIT 1'
            ),
        )
    )


BACKGROUND_SCENARIOS = frozenset(
    {
        "activity",
        "social_autonomy",
        "memory_extract",
        "memory_review",
        "diary",
        "reflection",
        "rem",
        "meme_caption",
        "qq_poke",
        "watch_review",
        "style_distill",
        "jargon",
    }
)
CAPACITY_WAIT_CODES = frozenset(
    {"MODEL_PROVIDER_CONCURRENCY_EXHAUSTED", "BACKGROUND_YIELD_TO_CHAT"}
)


def execution_counts(store, closures=()):
    """Count native admitted attempts, excluding only verified execution closures."""
    closed = sorted(closures)
    return store.one(
        "SELECT COUNT(*) AS total,COUNT(*) FILTER (WHERE m.scenario=ANY(%s::text[]) "
        "AND (m.scenario!='memory_review' OR m.job_id IS NULL)) AS background "
        "FROM model_usage m JOIN model_admissions a USING(request_id) "
        "WHERE m.state='RESERVED'"
        " AND m.request_id<>ALL(%s::text[])",
        (sorted(BACKGROUND_SCENARIOS), closed),
    )


def is_background(scenario, job_id=None):
    return scenario in BACKGROUND_SCENARIOS and not (scenario == "memory_review" and job_id)


def capacity_exhausted(store, approval, closures=(), *, scenario="chat", job_id=None):
    counts = execution_counts(store, closures)
    return (
        counts["total"] >= approval.concurrency
        or is_background(scenario, job_id)
        and counts["background"] >= max(1, approval.concurrency - 1)
    )


def capacity_wait(store, code, *, request_id=None):
    """Only an unadmitted or proven unsent attempt may wait, never an uncertain call."""
    return code in CAPACITY_WAIT_CODES and (
        request_id is None
        or not store.one(
            "SELECT 1 FROM model_usage WHERE request_id = %s",
            (request_id,),
        )
        or not_dispatched(store, request_id)
    )


def not_dispatched(store, request_id):
    return bool(
        store.one(
            (
                'SELECT 1 FROM model_usage AS m JOIN audit_log AS a ON '
                'a.entity_id = m.request_id WHERE m.request_id = %s AND '
                "m.state = 'SETTLED' AND m.billed_micro_usd = 0 AND "
                'm.input_tokens = 0 AND m.output_tokens = 0 AND '
                'm.is_estimate = 0 AND a.action = '
                "'model.not_dispatched' LIMIT 1"
            ),
            (request_id,),
        )
    )


def close_expired_model_work(store, table, now):
    """Close the interrupted business result; the original usage row owns the charge."""
    if table not in {"watch_ledger", "style_pairs", "jargon_work"}:
        raise ValueError("unknown model work table")
    store.execute(
        f"UPDATE {table} SET state=CASE u.state WHEN 'UNKNOWN' THEN 'UNKNOWN' ELSE 'FAILED' END,"
        "error_code=CASE u.state WHEN 'UNKNOWN' THEN 'MODEL_USAGE_UNKNOWN' "
        "ELSE 'MODEL_RESULT_NOT_CHECKPOINTED' END,lease_until=NULL,updated_at=%s "
        f"FROM model_usage u WHERE {table}.state='RUNNING' AND lease_until<=%s "
        "AND model_request_id=u.request_id AND u.state IN ('UNKNOWN','SETTLED')",
        (now, now),
    )


def check_request_limits(approval, measured, scenario):
    """Apply the approved local input/output and routed joint window to one measurement.

    An estimate remains an estimate; this does not attest remote tokenization.
    A separate video route never inherits the chat model's context window.
    """
    if approval is None:
        return
    if measured.reserved_input_tokens > approval.max_input_tokens:
        raise CoreError("MODEL_INPUT_LIMIT", 429)
    if measured.output_allowance > approval.max_output_tokens:
        raise CoreError("MODEL_OUTPUT_LIMIT", 429)
    route = approval.video_input if scenario in VIDEO_SCENARIOS else approval
    limit = route.max_context_tokens if route is not None else None
    if limit is not None and measured.reserved_input_tokens + measured.output_allowance > limit:
        raise CoreError("MODEL_CONTEXT_LIMIT", 429)


def _period_starts(now, timezone):
    local = datetime.fromisoformat(now).astimezone(ZoneInfo(timezone))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return day.astimezone(UTC), day.replace(day=1).astimezone(UTC)



def _check_period_limits(store, settings, approval, scenario, estimate, day, month, *, autonomous):
    """Financial totals are informational; feature and scheduler limits remain explicit."""
    if not autonomous:
        return
    if scenario not in paid_budget.daily_limits(settings):
        raise CoreError("UNKNOWN_BUDGET_SCENARIO", 400)
    feature = {
        "style_distill": settings.style_distill, "jargon": settings.jargon,
        "watch_review": settings.watch_ledger, "social_autonomy": settings.social_autonomy,
    }.get(scenario)
    if feature is not None and not feature.enabled:
        raise CoreError("SCENARIO_BUDGET_DISABLED", 429)
    if scenario in {"reflection", "social_autonomy"}:
        calls = store.one("SELECT count(*) AS n FROM model_usage WHERE occurred_at>=%s "
                          "AND scenario=%s",
                          (day.isoformat(timespec="microseconds"), scenario))["n"]
        limit = (settings.reflection_max_daily_calls if scenario == "reflection"
                 else settings.social_autonomy.model_daily_calls)
        if calls >= limit:
            raise CoreError("SCENARIO_CALL_LIMIT", 429)


def reserve(
    store,
    settings,
    job_id,
    scenario,
    messages,
    now,
    *,
    request_id=None,
    logical_request_id=None,
    clock=None,
    cloud_task_kind=None,
    cloud_task_id=None,
    source_check=None,
    tools=(),
    generation=None,
):
    from companion import runtime_config

    current_time = clock or (lambda: now)
    wire = (runtime_config.video_settings(store, settings, "job:" + job_id, scenario,
                                          clock=current_time) if job_id else settings)
    approval = paid_budget.contract(settings, now) if settings.mode == "live" else None
    inputs = model_scope.prepare_input(store, settings, current_time, wire, approval, request_id,
                                       logical_request_id, job_id, scenario, messages, tools,
                                       cloud_task_kind, cloud_task_id)
    logical_request_id = inputs.logical_id
    if inputs.video_binding is not None:
        cloud_task_kind, cloud_task_id = "video", inputs.video_binding["task_id"]
    autonomous = scenario not in {"chat", "qq_poke", "evaluation", "gateway"}
    day, month = _period_starts(now, settings.timezone)
    measurement = measure_request(wire, messages, scenario, tools=tools, generation=generation)
    estimated_input = measurement.reserved_input_tokens
    output_tokens = measurement.output_allowance
    check_request_limits(wire.paid_model, measurement, scenario)
    receipt = measurement.receipt()
    if wire is not settings:
        receipt["video_profile"] = wire.video_models.selected
    estimate = cost(wire, estimated_input, output_tokens, scenario=scenario)
    with store.transaction():
        configuration = runtime_config.bind(store, settings, current_time,
                                           "job:" + job_id) if job_id is not None else None

        model_scope.check_sources(store, settings, current_time, job_id, messages,
                                  inputs, source_check)
        if job_id is None and scenario in {"memory_extract", "memory_review"} and chat_pending(
            store, clock() if callable(clock) else now,
        ):
            raise CoreError("BACKGROUND_YIELD_TO_CHAT", 409)
        activate_cumulative = bool(
            approval
            and (
                approval.cumulative
                or store.one(
                    "SELECT 1 FROM runtime_meta WHERE key = %s",
                    (cumulative.PROJECTION,),
                )
            )
        )
        if activate_cumulative:
            now = clock() if callable(clock) else now
            day, month = _period_starts(now, settings.timezone)
        owner_view = model_scope.admit(store, settings, clock, now, job_id, scenario, request_id,
                                       inputs, messages, estimate, estimated_input)
        request_id = request_id or uid("model")
        if store.one("SELECT 1 FROM model_usage WHERE request_id = %s", (request_id,)):
            raise CoreError("MODEL_REQUEST_ALREADY_RESERVED", 409)
        model_scope.admit_attempt(store, settings, current_time, now, job_id, scenario,
                                 request_id, inputs, messages, source_check)
        _check_period_limits(store, settings, approval, scenario, estimate, day, month,
                             autonomous=autonomous)
        if approval:
            logical_request_id = logical_request_id or job_id or uid("logical")
            _admit(store, settings, approval, logical_request_id, estimate, now, day, month,
                   scenario=scenario, job_id=job_id)
        cloud_pricing = video_route(wire, scenario) or settings
        cloud_budget.reserve(
            store,
            settings,
            reference_type="model",
            reference_id=request_id,
            estimated_micro_usd=estimate,
            now=now,
            task_kind=cloud_task_kind or ("video" if scenario in VIDEO_SCENARIOS else "model"),
            task_id=cloud_task_id or logical_request_id or job_id or request_id,
            pricing={
                "pricing_known": bool(settings.pricing_known),
                **{
                    key: getattr(cloud_pricing, key)
                    for key in ("input_rate", "cached_input_rate", "output_rate")
                },
            },
        )
        if approval:
            # All monetary admission checks precede any journal initialization;
            # rejecting the CNY limit must not leave an orphaned fsynced header.
            if activate_cumulative:
                cumulative.activate(store, settings, now)
            quota = cumulative.admit_model(store, settings, request_id, logical_request_id, now,
                                            estimate, estimated_input, output_tokens)
        from companion.model import model_route

        provider, name, _ = model_route(wire, scenario)
        store.insert(
            "model_usage",
            usage_id=uid("usage"),
            request_id=request_id,
            job_id=job_id,
            scenario=scenario,
            provider=provider,
            model=name,
            input_tokens=estimated_input,
            output_tokens=output_tokens,
            billed_micro_usd=estimate,
            is_estimate=1,
            pricing_known=int(settings.pricing_known),
            state="RESERVED",
            occurred_at=now,
        )
        if configuration is not None:
            runtime_config.bind(store, settings, current_time, "model:" + request_id,
                                snapshot=configuration)
        if approval:
            from companion.model import model_payload

            payload = model_payload(wire, messages, scenario, tools=tools, generation=generation)
            _record_paid_reservation(
                store, settings, approval, scenario, request_id, logical_request_id,
                estimate, measurement, receipt, paid_budget.sha(payload), quota, now,
            )
            model_scope.freeze_authority(store, settings, clock, scenario,
                                         job_id, request_id, owner_view)
        return request_id


def _record_paid_reservation(store, settings, approval, scenario, request_id, logical_request_id,
                             estimate, measurement, receipt, payload_sha256, quota, now):
    """Persist the priced admission and append its independent proof before the caller commits."""
    snapshot = paid_budget.snapshot(settings)
    contract_hash = paid_budget.sha(snapshot)
    store.insert(
        "model_admissions",
        request_id=request_id,
        logical_request_id=logical_request_id,
        attempt_id=uid("attempt"),
        attempt_number=1
        + store.one(
            'SELECT COUNT(*) AS n FROM model_admissions WHERE logical_request_id=%s',
            (logical_request_id,),
        )["n"],
        window_id=approval.window_id,
        contract_sha256=contract_hash,
        payload_sha256=payload_sha256,
        snapshot_json=encode(snapshot),
        measurement_json=encode(receipt),
        reserved_micro_usd=estimate,
        input_bound=measurement.reserved_input_tokens,
        output_bound=measurement.output_allowance,
        journal_path=str(Path(approval.journal_path).resolve()),
        journal_id=store.one(
            "SELECT value FROM runtime_meta WHERE key='model_budget_journal_id'"
        )["value"],
        lease_token=uid("model_lease"),
        lease_until=after(now, settings.model_timeout(scenario) + 10),
    )
    paid_budget.journal(
        store,
        approval,
        {
            "kind": "reserve",
            "state": "RESERVED",
            "request_id": request_id,
            "minimum_micro_usd": estimate,
            "contract_sha256": contract_hash,
            "measurement": {"version": 1, "sha256": paid_budget.sha(
                receipt)},
            **({"quota": quota} if quota else {}),
            **model_scope.journal_binding(store, scenario, request_id),
        },
    )



def _admit(store, settings, approval, logical, estimate, now, day, month, *, scenario="chat",
           job_id=None):
    """Keep contract identity and live execution capacity without monetary refusals."""
    if store.one(
        "SELECT 1 FROM model_usage AS m LEFT JOIN model_admissions AS a USING(request_id) "
        "WHERE a.request_id IS NULL AND m.state='RESERVED' LIMIT 1"
    ):
        raise CoreError("MODEL_LEGACY_USAGE_RECONCILIATION_REQUIRED", 503)
    frozen = paid_budget.sha(paid_budget.snapshot(settings))
    prior = store.one("SELECT contract_sha256 FROM model_budget_windows WHERE window_id=%s",
                      (approval.window_id,))
    if prior and prior["contract_sha256"] != frozen:
        raise CoreError("PAID_WINDOW_CONTRACT_CHANGED", 503)
    store.execute("INSERT INTO model_budget_windows VALUES (%s,%s) ON CONFLICT DO NOTHING",
                  (approval.window_id, frozen))
    if capacity_exhausted(store, approval, scenario=scenario, job_id=job_id):
        raise CoreError("MODEL_PROVIDER_CONCURRENCY_EXHAUSTED", 429)


def mark_reservation_unknown(store, request_id):
    store.execute(
        "UPDATE model_usage SET state = 'UNKNOWN' WHERE request_id = %s AND state = 'RESERVED'",
        (request_id,),
    )


def settle(store, settings, request_id, response=None, *, transport_failure=None):
    with nullcontext() if store.in_write_transaction else store.transaction():
        usage = store.one(
            "SELECT occurred_at FROM model_usage WHERE request_id = %s", (request_id,)
        )
        if usage:
            cloud_budget.sync(store, settings, usage["occurred_at"])
        admission = store.one(
            "SELECT model_admissions.request_id AS request_id, "
            "model_admissions.logical_request_id AS logical_request_id, "
            "model_admissions.attempt_id AS attempt_id, model_admissions.attempt_number AS "
            "attempt_number, model_admissions.window_id AS window_id, "
            "model_admissions.contract_sha256 AS contract_sha256, model_admissions.payload_sha256 "
            "AS payload_sha256, model_admissions.snapshot_json AS snapshot_json, "
            "model_admissions.reserved_micro_usd AS reserved_micro_usd, "
            "model_admissions.input_bound AS input_bound, model_admissions.output_bound AS "
            "output_bound, model_admissions.lease_token AS lease_token, "
            "model_admissions.lease_until AS lease_until, model_admissions.io_started_at AS "
            "io_started_at, model_admissions.journal_path AS journal_path, "
            "model_admissions.journal_id AS journal_id, model_admissions.measurement_json AS "
            "measurement_json, model_admissions.authority_json AS authority_json FROM "
            "model_admissions AS model_admissions WHERE model_admissions.request_id = "
            "%s",
            (request_id,),
        )
        if admission:
            _settle_paid(store, settings, admission, response, transport_failure=transport_failure)
            return
        if response is None or response.input_tokens is None or response.output_tokens is None:
            store.execute(
                "UPDATE model_usage SET state = 'UNKNOWN' WHERE request_id = %s", (request_id,)
            )
        else:
            store.execute(
                "UPDATE model_usage SET input_tokens = %s, output_tokens = %s, cached_input_tokens "
                "= %s, billed_micro_usd = %s, state = 'SETTLED' WHERE request_id = %s",
                (
                    response.input_tokens,
                    response.output_tokens,
                    response.cached_tokens,
                    cost(
                        cloud_budget.model_pricing(store, request_id, settings),
                        response.input_tokens,
                        response.output_tokens,
                        response.cached_tokens,
                    ),
                    request_id,
                ),
            )
        # Configured rates make monetary amounts estimates even with observed usage.
        cloud_budget.sync_reference(store, "model", request_id)


def expire_cancelled_reservations(store, settings, now):
    """Recover abandoned cancellation drains without losing their real liability."""
    if not store.in_transaction:
        raise RuntimeError("BUDGET_RECOVERY_TRANSACTION_REQUIRED")
    for admission in store.rows(
        "SELECT a.request_id AS request_id, a.logical_request_id AS logical_request_id, "
        "a.attempt_id AS attempt_id, a.attempt_number AS attempt_number, a.window_id AS window_id, "
        "a.contract_sha256 AS contract_sha256, a.payload_sha256 AS payload_sha256, a.snapshot_json "
        "AS snapshot_json, a.reserved_micro_usd AS reserved_micro_usd, a.input_bound AS "
        "input_bound, a.output_bound AS output_bound, a.lease_token AS lease_token, a.lease_until "
        "AS lease_until, a.io_started_at AS io_started_at, a.journal_path AS journal_path, "
        "a.journal_id AS journal_id, a.measurement_json AS measurement_json, a.authority_json AS "
        "authority_json FROM model_admissions AS a JOIN model_usage AS m ON a.request_id = "
        "m.request_id JOIN jobs AS j ON j.job_id = m.job_id WHERE j.state = 'CANCELLED' AND "
        "m.state = 'RESERVED' AND a.lease_until <= %s",
        (now,),
    ):
        _settle_paid(store, settings, admission, None)


def _settle_unstarted_reply(store, request_id, dispatch, reason, now):
    """Record known zero provider I/O; keep the admitted call and its audit proof."""
    with store.transaction():
        admission = store.one(
            "SELECT model_admissions.request_id AS request_id, "
            "model_admissions.logical_request_id AS logical_request_id, "
            "model_admissions.attempt_id AS attempt_id, model_admissions.attempt_number AS "
            "attempt_number, model_admissions.window_id AS window_id, "
            "model_admissions.contract_sha256 AS contract_sha256, model_admissions.payload_sha256 "
            "AS payload_sha256, model_admissions.snapshot_json AS snapshot_json, "
            "model_admissions.reserved_micro_usd AS reserved_micro_usd, "
            "model_admissions.input_bound AS input_bound, model_admissions.output_bound AS "
            "output_bound, model_admissions.lease_token AS lease_token, "
            "model_admissions.lease_until AS lease_until, model_admissions.io_started_at AS "
            "io_started_at, model_admissions.journal_path AS journal_path, "
            "model_admissions.journal_id AS journal_id, model_admissions.measurement_json AS "
            "measurement_json, model_admissions.authority_json AS authority_json FROM "
            "model_admissions AS model_admissions WHERE model_admissions.request_id = "
            "%s",
            (request_id,),
        )
        usage = store.one(
            "SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS "
            "request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, "
            "model_usage.provider AS provider, model_usage.model AS model, "
            "model_usage.input_tokens AS input_tokens, model_usage.cached_input_tokens AS "
            "cached_input_tokens, model_usage.output_tokens AS output_tokens, "
            "model_usage.billed_micro_usd AS billed_micro_usd, model_usage.is_estimate AS "
            "is_estimate, model_usage.occurred_at AS occurred_at, model_usage.state AS state, "
            "model_usage.pricing_known AS pricing_known FROM model_usage AS model_usage WHERE "
            "model_usage.request_id = %s",
            (request_id,),
        )
        if (
            not admission
            or not usage
            or usage["state"] != "RESERVED"
            or dispatch["dispatched"]
            or (
                admission["io_started_at"] is not None
                and (
                    not dispatch.get("owned")
                    or admission["io_started_at"] != dispatch.get("io_started_at")
                )
            )
        ):
            return False
        evidence = paid_budget.sha(
            {
                "kind": "reply_not_dispatched",
                "request_id": request_id,
                "lease_token": admission["lease_token"],
                "payload_sha256": admission["payload_sha256"],
                "reason": reason,
            }
        )
        _persist_accounting(store, admission, 0, "SETTLED", False, evidence, tokens=(0, 0))
        store.execute(
            "UPDATE model_usage SET input_tokens = 0, output_tokens = 0, "
            "cached_input_tokens = 0, is_estimate = 0 WHERE request_id = %s AND state = 'SETTLED'",
            (request_id,),
        )
        store.audit(
            "model.not_dispatched",
            request_id,
            "budget",
            now,
            reason=reason,
            evidence_sha256=evidence,
        )
        return True


def _settle_paid(store, settings, admission, response, *, transport_failure=None):
    usage = store.one(
        "SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS "
        "request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, "
        "model_usage.provider AS provider, model_usage.model AS model, model_usage.input_tokens AS "
        "input_tokens, model_usage.cached_input_tokens AS cached_input_tokens, "
        "model_usage.output_tokens AS output_tokens, model_usage.billed_micro_usd AS "
        "billed_micro_usd, model_usage.is_estimate AS is_estimate, model_usage.occurred_at AS "
        "occurred_at, model_usage.state AS state, model_usage.pricing_known AS pricing_known FROM "
        "model_usage AS model_usage WHERE model_usage.request_id = %s",
        (admission["request_id"],),
    )
    if usage["state"] == "SETTLED":
        return
    try:
        snapshot = json.loads(admission["snapshot_json"])
    except (ValueError, TypeError):
        snapshot = None
    if not isinstance(snapshot, dict) or paid_budget.sha(snapshot) != admission["contract_sha256"]:
        _persist_accounting(
            store,
            admission,
            max(usage["billed_micro_usd"], admission["reserved_micro_usd"]),
            "UNKNOWN",
            True,
        )
        return
    profile = json.loads(admission["measurement_json"] or "{}").get("video_profile")
    rates = snapshot["video_input"] if usage["scenario"] in VIDEO_SCENARIOS else snapshot
    if profile is not None:
        rates = snapshot.get("video_routes", {}).get(profile)
        if not rates or (rates["provider"], rates["model"]) != (usage["provider"], usage["model"]):
            _persist_accounting(store, admission, admission["reserved_micro_usd"], "UNKNOWN", True)
            return
    pricing = SimpleNamespace(
        pricing_known=True,
        **{name: rates[name] for name in ("input_rate", "cached_input_rate", "output_rate")},
    )
    valid = (
        response is not None
        and all(
            type(value) is int and value >= 0
            for value in (response.input_tokens, response.output_tokens, response.cached_tokens)
        )
        and response.cached_tokens <= response.input_tokens
        and response.input_tokens > 0
        and response.output_tokens > 0
    )
    amount, state, blocked = (
        admission["reserved_micro_usd"],
        "UNKNOWN",
        response is not None and not valid,
    )
    diagnostic = {
        "code": "TRANSPORT_USAGE_UNKNOWN" if response is None else "USAGE_MISSING_OR_INVALID",
        "actual_usage_known": False,
    }
    if response is None and transport_failure is not None:
        diagnostic["transport_failure"] = transport_failure
    if valid:
        actual = cost(
            pricing, response.input_tokens, response.output_tokens, response.cached_tokens
        )
        # A reservation is an estimate. Small estimation errors settle normally;
        # actual local amount limits and the requested output cap remain hard stops.
        amount, state, blocked = actual, "SETTLED", False
        limits = _actual_limit_violations(store, admission, snapshot, usage, actual)
        if snapshot.get("cumulative") or store.one(
            "SELECT 1 FROM runtime_meta WHERE key = %s", (cumulative.PROJECTION,)
        ):
            try:
                view = paid_budget.journal(
                    store,
                    SimpleNamespace(journal_path=admission["journal_path"]),
                    allow_settlement=True,
                )["cumulative"]
                prior = view["models"][admission["request_id"]]
                proposed = prior | {
                    "micro_usd": actual,
                    "state": "SETTLED",
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                }
                limits.extend(
                    "CUMULATIVE_" + code
                    for code in cumulative.violations(
                        view, proposed, replacing="model:" + admission["request_id"]
                    )
                )
            except (CoreError, KeyError):
                limits.append("CUMULATIVE_CUSTODY_UNAVAILABLE")
        if response.output_tokens > admission["output_bound"]:
            limits.append("OUTPUT_CAP_EXCEEDED")
        spike = response.input_tokens > max(
            admission["input_bound"] * snapshot.get("usage_anomaly_ratio", 4),
            admission["input_bound"] + snapshot.get("usage_anomaly_min_tokens", 256),
        )
        diagnostic = {
            "code": (
                "LOCAL_LIMIT_EXCEEDED" if limits else "INPUT_USAGE_SPIKE" if spike else "NORMAL"
            ),
            "actual_usage_known": True,
            "input_estimated_tokens": admission["input_bound"],
            "input_actual_tokens": response.input_tokens,
            "input_error_tokens": response.input_tokens - admission["input_bound"],
            "output_actual_tokens": response.output_tokens,
            "cached_input_tokens": response.cached_tokens,
            "reserved_micro_usd": admission["reserved_micro_usd"],
            "usage_priced_micro_usd": actual,
            "cost_error_micro_usd": actual - admission["reserved_micro_usd"],
            "currency": snapshot["currency"],
            "billing_basis": "configured_rates_not_invoice",
            "limits": limits,
        }
        if limits:
            amount, state, blocked = max(actual, admission["reserved_micro_usd"]), "UNKNOWN", True
    current_pricing = settings
    if usage["scenario"] in VIDEO_SCENARIOS:
        current_pricing = settings.paid_model.video_input if settings.paid_model else None
        if profile is not None and settings.paid_model:
            current_pricing = settings.paid_model.video_routes.get(profile)
    if current_pricing is None or (
        current_pricing.input_rate,
        current_pricing.cached_input_rate,
        current_pricing.output_rate,
    ) != (pricing.input_rate, pricing.cached_input_rate, pricing.output_rate):
        amount, state, blocked = max(amount, admission["reserved_micro_usd"]), "UNKNOWN", True
        diagnostic["code"] = "PRICING_CONFIG_CHANGED"
    if usage["state"] == "UNKNOWN":
        amount, state = max(amount, usage["billed_micro_usd"]), "UNKNOWN"
    # Mutable enabling approval is deliberately irrelevant to accounting custody.
    _persist_accounting(
        store,
        admission,
        amount,
        state,
        blocked,
        tokens=(response.input_tokens, response.output_tokens) if valid else None,
    )
    if valid:
        store.execute(
            "UPDATE model_usage SET input_tokens = %s, output_tokens = %s, cached_input_tokens = "
            "%s WHERE request_id = %s",
            (
                response.input_tokens,
                response.output_tokens,
                response.cached_tokens,
                admission["request_id"],
            ),
        )
    present = getattr(response, "cache_usage_present", None)
    diagnostic["cache_usage_present"] = present if type(present) is bool else None
    store.audit(
        "model.usage_diagnostic",
        admission["request_id"],
        "budget",
        usage["occurred_at"],
        **diagnostic,
    )



def _actual_limit_violations(store, admission, snapshot, usage, actual):
    """Actual charges are recorded in full; financial ceilings no longer reject them."""
    return []


def usage_diagnostics(store):
    """Read-only calibration from settled paid usage; no prompts or account identifiers."""
    row = store.one(
        # Read correction IDs once instead of scanning the audit log for every call.
        "SELECT COUNT(*) AS calibrated_calls, COALESCE(CAST(SUM(a.input_bound) AS BIGINT), 0) AS "
        "input_estimated_tokens, COALESCE(CAST(SUM(m.input_tokens) AS BIGINT), 0) AS "
        "input_actual_tokens, COALESCE(CAST(SUM(m.output_tokens) AS BIGINT), 0) AS "
        "output_actual_tokens, COALESCE(CAST(SUM(m.cached_input_tokens) AS BIGINT), 0) AS "
        "cached_input_tokens, COALESCE(CAST(SUM(a.reserved_micro_usd) AS BIGINT), 0) AS "
        "reserved_micro_usd, COALESCE(CAST(SUM(m.billed_micro_usd) AS BIGINT), 0) AS "
        "usage_priced_micro_usd FROM model_admissions AS a JOIN model_usage AS m USING "
        "(request_id) WHERE m.state = 'SETTLED' AND NOT m.request_id IN (SELECT entity_id FROM "
        "audit_log WHERE action = 'model.video_rate_correction' AND NOT entity_id IS NULL)"
    )
    # Diagnostics never reinterpret the reserved tokens of an UNKNOWN as observed usage.
    anomalies = store.one(

            'SELECT COUNT(DISTINCT entity_id) AS n FROM audit_log WHERE '
            "action='model.usage_diagnostic' AND (CASE WHEN "
            'safe_detail_json IS JSON THEN '
            "(safe_detail_json::json#>>'{code}') END) IN "
            "('INPUT_USAGE_SPIKE','LOCAL_LIMIT_EXCEEDED',"
            "'USAGE_MISSING_OR_INVALID')"

    )["n"]
    return {
        **row,
        "input_error_tokens": row["input_actual_tokens"] - row["input_estimated_tokens"],
        "cost_error_micro_usd": row["usage_priced_micro_usd"] - row["reserved_micro_usd"],
        "anomaly_calls": anomalies,
        "currency": "USD" if row["calibrated_calls"] else None,
        "billing_basis": "configured_rates_not_invoice",
    }


def _persist_accounting(
    store,
    admission,
    amount,
    state,
    blocked,
    evidence_sha256=None,
    *,
    tokens=None,
):
    """Record known costs even when approval is disabled or journal custody fails.

    No journal failure may roll back a known larger charge. The DB stop is committed;
    a damaged/missing independent journal additionally prevents future admission.
    """
    custody = SimpleNamespace(journal_path=admission["journal_path"])
    journal_failed = False
    quota = None
    try:
        identity = store.one("SELECT value FROM runtime_meta WHERE key = 'model_budget_journal_id'")
        if not identity or identity["value"] != admission["journal_id"]:
            raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)
        paid_budget.journal(store, custody, allow_settlement=True)
        quota = cumulative.admission_metadata(
            store, admission, amount=amount, state=state, tokens=tokens
        )
    except CoreError:
        journal_failed = True
    if journal_failed:
        amount, state, blocked = max(amount, admission["reserved_micro_usd"]), "UNKNOWN", True
    store.execute(
        "UPDATE model_usage SET billed_micro_usd = %s, state = %s WHERE request_id = %s",
        (amount, state, admission["request_id"]),
    )
    if not journal_failed:
        record = {
            "kind": "blocked" if blocked else "settle",
            "state": state,
            "request_id": admission["request_id"],
            "minimum_micro_usd": amount,
            "contract_sha256": admission["contract_sha256"],
            "io_started_at": admission["io_started_at"],
            **({"quota": quota} if quota else {}),
        }
        if evidence_sha256:
            record["evidence_sha256"] = evidence_sha256
        try:
            paid_budget.journal(store, custody, record, allow_settlement=True)
        except CoreError:
            amount, state, blocked = max(amount, admission["reserved_micro_usd"]), "UNKNOWN", True
            store.execute(
                "UPDATE model_usage SET billed_micro_usd = %s, state = %s WHERE request_id = %s",
                (amount, state, admission["request_id"]),
            )
    if blocked:
        store.execute(
            "INSERT INTO runtime_meta (key, value) VALUES ('model_budget_blocked', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, record_sequence = DEFAULT"
        )
    cloud_budget.sync_reference(store, "model", admission["request_id"])


def _check_measurement(
    settings,
    admission,
    messages,
    scenario,
    *,
    tools=(),
    generation=None,
    video_profile=None,
    prepared=None,
):
    if prepared is None:
        measured = measure_request(settings, messages, scenario, tools=tools, generation=generation)
    else:
        from companion.model import model_payload

        measured, contract = prepared
        if contract != paid_budget.sha(
            paid_budget.snapshot(settings)
        ) or measured.payload_sha256 != paid_budget.sha(
            model_payload(settings, messages, scenario, tools=tools, generation=generation)
        ):
            raise CoreError("MODEL_TOKEN_MEASUREMENT_CHANGED", 503)
    check_request_limits(settings.paid_model, measured, scenario)
    if measured.reserved_input_tokens != admission["input_bound"]:
        raise CoreError("MODEL_INPUT_ESTIMATE_CHANGED", 503)
    saved = json.loads(admission["measurement_json"])
    if saved:
        expected = measured.receipt()
        if video_profile is not None:
            expected["video_profile"] = video_profile
        if saved != expected or saved["payload_sha256"] != admission["payload_sha256"]:
            raise CoreError("MODEL_TOKEN_MEASUREMENT_CHANGED", 503)
    elif settings.token_measurement is not None:
        # Empty migrated rows mean historical estimates; never upgrade their evidence.
        raise CoreError("MODEL_TOKEN_MEASUREMENT_CHANGED", 503)


async def complete(
    model,
    store,
    settings,
    request_id,
    messages,
    scenario,
    clock,
    *,
    source_check=None,
    tools=(),
    trace_id=None,
    generation=None,
):
    """Dispatch a reservation and settle every outcome before returning to its caller."""
    from companion.model import (
        ModelClient,
        ModelDispatchRefused,
        ModelResponseError,
        ModelTransportError,
    )
    from companion.reply_revision import CANCELLED, SOURCE_RECALLED, SUPERSEDED

    response = None
    transport_failure = None
    dispatch = {"dispatched": False}
    try:
        response = await _dispatch(
            model,
            store,
            settings,
            request_id,
            messages,
            scenario,
            clock,
            dispatch=dispatch,
            source_check=source_check,
            tools=tools,
            trace_id=trace_id,
            generation=generation,
        )
        return response
    except ModelResponseError as error:
        # An unusable reply can still carry observed usage.
        response = error.response
        raise
    except CoreError as error:
        if isinstance(error, ModelTransportError):
            transport_failure = error.failure
        known_not_sent = isinstance(
            error, ModelDispatchRefused
        ) and error.admission is dispatch.get("admission")
        before_io_yield = error.code == "BACKGROUND_YIELD_TO_CHAT" and not dispatch.get("owned")
        if isinstance(model, ModelClient) and (
            known_not_sent
            or before_io_yield
            or settings.mode == "live"
            and error.code in {CANCELLED, SOURCE_RECALLED, SUPERSEDED}
        ):
            _settle_unstarted_reply(store, request_id, dispatch, error.code, clock())
        raise
    finally:
        # Cancellation is an unknown outcome too; callers never refund a started attempt.
        settle(store, settings, request_id, response, transport_failure=transport_failure)
        from companion import model_carry

        if model_carry.active(settings) and scenario != "gateway":
            model_carry.close_owned(store, settings, request_id, dispatch, clock())


def _start_paid_attempt(store, settings, clock, approval, request_id, scenario, now, check_payload):
    """Bind one usage row and journal I/O start under the caller's writer lock."""
    paid_budget.contract(settings, clock())
    custody = paid_budget.journal(store, approval)
    cumulative.check(store, settings, clock(), custody=custody)
    row = store.one(
        (
            (
                'SELECT a.request_id, a.logical_request_id, a.attempt_id, '
                'a.attempt_number, a.window_id, a.contract_sha256, '
                'a.payload_sha256, a.snapshot_json, a.reserved_micro_usd, '
                'a.input_bound, a.output_bound, a.lease_token, a.lease_until,'
                ' a.io_started_at, a.journal_path, a.journal_id, '
                'a.measurement_json, a.authority_json,m.state,m.scenario AS '
                'usage_scenario,m.job_id AS usage_job_id FROM '
                'model_admissions a JOIN model_usage m USING(request_id) '
                'WHERE request_id=%s'
            )
        ),
        (request_id,),
    )
    if not row or row["io_started_at"] or row["state"] != "RESERVED":
        raise CoreError("MODEL_REQUEST_ALREADY_STARTED", 409)
    if row["usage_scenario"] != scenario:
        raise CoreError("MODEL_ADMISSION_PAYLOAD_CHANGED", 503)
    model_scope.check_media(store, settings, clock, request_id, scenario, row)
    if datetime.fromisoformat(row["lease_until"]) <= datetime.fromisoformat(now):
        raise CoreError("MODEL_ADMISSION_LEASE_EXPIRED", 503)
    check_payload(row)
    quota = cumulative.admission_metadata(store, row)
    if quota:
        now = clock()
        cumulative.check(store, settings, now)
        if datetime.fromisoformat(row["lease_until"]) <= datetime.fromisoformat(now):
            raise CoreError("MODEL_ADMISSION_LEASE_EXPIRED", 503)
    store.execute(
        'UPDATE model_admissions SET io_started_at=%s WHERE request_id=%s', (now, request_id)
    )
    paid_budget.journal(
        store,
        approval,
        {
            "kind": "io_started",
            "state": "RESERVED",
            "request_id": request_id,
            "minimum_micro_usd": row["reserved_micro_usd"],
            "contract_sha256": row["contract_sha256"],
            "io_started_at": now,
            **({"quota": quota} if quota else {}),
        },
    )
    return row, now


def _transport_custody(store, settings, clock, approval, request_id, scenario, row, now):
    """Reacquire the original lease and scope after all user HTTP hooks have returned."""
    from companion import notices, runtime_config

    runtime_config.check_request(store, settings, request_id)
    custody = paid_budget.journal(store, approval)
    quota_state = cumulative.check(store, settings, clock(), custody=custody)
    current_job = model_scope.check(store, settings, clock, request_id, scenario)
    notices.check_request(store, settings, request_id, clock())
    current = store.one(
        (
            (
                'SELECT a.request_id, a.logical_request_id, a.attempt_id, '
                'a.attempt_number, a.window_id, a.contract_sha256, '
                'a.payload_sha256, a.snapshot_json, a.reserved_micro_usd, '
                'a.input_bound, a.output_bound, a.lease_token, a.lease_until,'
                ' a.io_started_at, a.journal_path, a.journal_id, '
                'a.measurement_json, '
                'a.authority_json,m.state,m.scenario,m.job_id AS usage_job_id'
                ' FROM model_admissions a JOIN model_usage m '
                'USING(request_id) WHERE request_id=%s'
            )
        ), (request_id,),
    )
    if (
        not current or current["state"] != "RESERVED"
        or current["lease_token"] != row["lease_token"]
        or current["io_started_at"] != now
        or current["payload_sha256"] != row["payload_sha256"]
        or current["contract_sha256"] != row["contract_sha256"]
        or current["measurement_json"] != row["measurement_json"]
        or current["scenario"] != scenario
        or current["usage_job_id"] != row["usage_job_id"]
        or current["logical_request_id"] != row["logical_request_id"]
        or current["authority_json"] != row["authority_json"]
    ):
        raise CoreError("MODEL_ADMISSION_LEASE_EXPIRED", 503)
    return current, current_job, quota_state


async def _dispatch(
    model,
    store,
    settings,
    request_id,
    messages,
    scenario,
    clock,
    *,
    dispatch=None,
    source_check=None,
    tools=(),
    trace_id=None,
    generation=None,
):
    """Persisted admission is fenced before adapter I/O."""
    from companion import notices, runtime_config
    from companion.telemetry import phase

    if source_check is not None:
        with phase("dispatch_sources", trace_id):
            source_check()
    with phase("dispatch_scope", trace_id):
        runtime_config.check_request(store, settings, request_id)
        model_scope.check(store, settings, clock, request_id, scenario)
        notices.check_request(store, settings, request_id, clock() if callable(clock) else clock)
    if settings.mode != "live":
        with store.transaction():
            cloud_budget.mark_dispatched(
                store, "model", request_id, clock() if callable(clock) else clock
            )
        return await model.complete(messages, scenario, **({"tools": tools} if tools else {}))
    if not callable(clock):
        raise CoreError("MODEL_ADMISSION_CLOCK_REQUIRED", 503)
    now = clock()
    from companion.model import ModelAdmission, ModelClient, model_payload

    wire = runtime_config.video_settings(store, settings, "model:" + request_id, scenario)
    profile = wire.video_models.selected if wire is not settings else None

    model_scope.check_adapter(store, settings, wire, model, scenario)
    approval = paid_budget.contract(settings, now)
    # Pure counting can load/tokenize the full request. Keep it outside the writer;
    # reuse only inside this invocation while its exact wire/counting contract matches.
    counting_contract = paid_budget.sha(paid_budget.snapshot(wire))
    with phase("dispatch_measure", trace_id):
        prepared = (measure_request(wire, messages, scenario, tools=tools,
                                    generation=generation), counting_contract)

    def check_payload(admission):
        if admission["contract_sha256"] != paid_budget.sha(paid_budget.snapshot(settings)):
            raise CoreError("PAID_WINDOW_CONTRACT_CHANGED", 503)
        if admission["payload_sha256"] != paid_budget.sha(model_payload(
            wire, messages, scenario, tools=tools, generation=generation,
        )):
            raise CoreError("MODEL_ADMISSION_PAYLOAD_CHANGED", 503)
        _check_measurement(wire, admission, messages, scenario, tools=tools, generation=generation,
                           video_profile=profile, prepared=prepared)

    with phase("dispatch_journal", trace_id), store.transaction():
        row, now = _start_paid_attempt(store, settings, clock, approval, request_id, scenario,
                                       now, check_payload)
    if dispatch is not None:
        dispatch.update(owned=True, io_started_at=now)
        from companion import model_carry

        if model_carry.active(settings) and scenario != "gateway":
            dispatch["owner_tuple"] = model_carry._tuple({**row, "io_started_at": now})
    # Re-read after lock acquisition, input estimation and journal fsync; no stale
    # pre-lock timestamp may authorize an HTTP request after the deadline.
    with phase("dispatch_recheck", trace_id):
        paid_budget.contract(settings, clock())
        # I/O custody already began and this phase cannot prove non-dispatch, so a late
        # owner message is left to the HTTP-boundary check, which can.
        model_scope.check(store, settings, clock, request_id, scenario, yield_to_chat=False)
        if row["contract_sha256"] != paid_budget.sha(paid_budget.snapshot(settings)):
            raise CoreError("PAID_WINDOW_CONTRACT_CHANGED", 503)
        if datetime.fromisoformat(row["lease_until"]) <= datetime.fromisoformat(clock()):
            raise CoreError("MODEL_ADMISSION_LEASE_EXPIRED", 503)
    if isinstance(model, ModelClient):

        async def before_dispatch(request):
            # Every user hook has completed. Custody and current runtime checks
            # remain here; the adapter only binds the actual HTTP request bytes.
            with phase("transport_custody", trace_id), store.transaction():
                current, current_job, quota_state = _transport_custody(
                    store, settings, clock, approval, request_id, scenario, row, now,
                )
            paid_budget.contract(settings, clock())
            model_scope.check_media(store, settings, clock, request_id, scenario, row)
            with phase("transport_payload", trace_id):
                check_payload(current)
            if source_check is not None:
                # Caller-owned evidence may expire while an HTTP hook awaits.
                # Keep this synchronous proof inside the existing transport fence.
                with phase("transport_sources", trace_id), store.transaction():
                    source_check()
            with store.transaction():
                cloud_budget.mark_dispatched(store, "model", request_id, clock())
            model_scope.transport_fence(store, settings, clock, request_id, scenario, model,
                                        request.url, current, current_job, quota_state)
            if dispatch is not None:
                # The checked model transport calls its delegate immediately after
                # this hook; no await separates the final proof from that handoff.
                dispatch["dispatched"] = True

        admission = ModelAdmission(row["payload_sha256"], before_dispatch, trace_id=trace_id)
        if dispatch is not None:
            dispatch["admission"] = admission
        try:
            if wire is not settings:
                model = ModelClient(wire, model.client)
            return await model.complete(
                messages,
                scenario,
                admission=admission,
                **({"tools": tools} if tools else {}),
                **({"generation": generation} if generation else {}),
            )
        finally:
            if dispatch is not None:
                dispatch["transport_returned"] = True
    with store.transaction():
        cloud_budget.mark_dispatched(store, "model", request_id, clock())
    return await model.complete(messages, scenario, **({"tools": tools} if tools else {}))


def record_provider_discrepancy(
    store, settings, request_id, actual_micro_usd, currency, evidence_sha256
):
    """Private operator reconciliation input; never clears a stop or authorizes I/O.

    Currency/FX uncertainty stops admission without inventing a conversion. The
    operator must independently verify the statement and keep its private evidence.
    """
    if (
        type(actual_micro_usd) is not int
        or actual_micro_usd < 0
        or not re.fullmatch(r"[a-f0-9]{64}", evidence_sha256)
    ):
        raise CoreError("INVALID_PROVIDER_RECONCILIATION", 400)
    with store.transaction():
        row = store.one(
            "SELECT a.request_id AS request_id, a.logical_request_id AS logical_request_id, "
            "a.attempt_id AS attempt_id, a.attempt_number AS attempt_number, a.window_id AS "
            "window_id, a.contract_sha256 AS contract_sha256, a.payload_sha256 AS payload_sha256, "
            "a.snapshot_json AS snapshot_json, a.reserved_micro_usd AS reserved_micro_usd, "
            "a.input_bound AS input_bound, a.output_bound AS output_bound, a.lease_token AS "
            "lease_token, a.lease_until AS lease_until, a.io_started_at AS io_started_at, "
            "a.journal_path AS journal_path, a.journal_id AS journal_id, a.measurement_json AS "
            "measurement_json, a.authority_json AS authority_json, m.billed_micro_usd AS "
            "billed_micro_usd FROM model_admissions AS a JOIN model_usage AS m ON a.request_id = "
            "m.request_id WHERE COALESCE(a.request_id, m.request_id) = %s",
            (request_id,),
        )
        if row is None:
            raise CoreError("MODEL_REQUEST_NOT_FOUND", 404)
        amount = max(
            row["billed_micro_usd"],
            row["reserved_micro_usd"],
            actual_micro_usd if currency == "USD" else 0,
        )
        _persist_accounting(store, row, amount, "UNKNOWN", True, evidence_sha256)
