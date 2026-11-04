import json
import re
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx

from companion import cloud_budget, cumulative, paid_budget
from companion.contracts import CoreError
from companion.store import after, encode, uid
from companion.token_measurement import byte_estimate, measure_request
from companion.video_input import SCENARIOS as VIDEO_SCENARIOS


def token_upper_bound(messages):
    # Conservative byte bound, not a tokenizer measurement; actual usage replaces it.
    return byte_estimate(messages)


def cost(settings, input_tokens, output_tokens, cached=0, *, scenario=None):
    if scenario in VIDEO_SCENARIOS and settings.paid_model and settings.paid_model.video_input:
        settings = SimpleNamespace(pricing_known=True,
                                   **settings.paid_model.video_input.model_dump())
    if not settings.pricing_known:
        return 0
    numerator = (
        (input_tokens - cached) * settings.input_rate
        + cached * settings.cached_input_rate
        + output_tokens * settings.output_rate
    )
    return (numerator + 999999) // 1000000


def total_monthly(store, month, before=None):
    """Shared cost ceiling includes unknown/reserved model and isolated tool charges."""
    total = store.one(
        "SELECT COALESCE(SUM(billed_micro_usd),0) AS n FROM model_usage WHERE occurred_at>=?"
        + (" AND occurred_at<?" if before else ""),
        (month, before) if before else (month,),
    )["n"]
    if store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_tasks'"):
        total += store.one(
            "SELECT COALESCE(SUM(billed_micro_usd),0) AS n FROM tool_tasks WHERE dispatched_at>=?"
            + (" AND dispatched_at<?" if before else ""),
            (month, before) if before else (month,),
        )["n"]
    return total


def chat_pending(store, now):
    return bool(store.one(
        "SELECT 1 FROM jobs j JOIN events e ON e.event_id=j.event_id "
        "WHERE j.kind='chat' AND e.redacted=0 "
        "AND j.state IN ('QUEUED','RUNNING','RETRY_WAIT') LIMIT 1",
    ))


BACKGROUND_SCENARIOS = frozenset({
    "activity", "social_autonomy", "memory_extract", "memory_review", "diary",
    "reflection", "rem", "meme_caption", "qq_poke", "watch_review", "style_distill", "jargon",
})
CAPACITY_WAIT_CODES = frozenset({"MODEL_PROVIDER_CONCURRENCY_EXHAUSTED",
                                 "BACKGROUND_YIELD_TO_CHAT"})


def execution_counts(store, closures=()):
    """Count native admitted attempts, excluding only verified execution closures."""
    closed = sorted(closures)
    return store.one(
        "SELECT COUNT(*) AS total,COALESCE(SUM(m.scenario IN ("
        + ",".join("?" for _ in BACKGROUND_SCENARIOS)
        + ") AND (m.scenario!='memory_review' OR m.job_id IS NULL)),0) AS background "
        "FROM model_usage m JOIN model_admissions a USING(request_id) "
        "WHERE m.state IN ('RESERVED','UNKNOWN')"
        + (" AND m.request_id NOT IN (" + ",".join("?" for _ in closed) + ")" if closed else ""),
        [*sorted(BACKGROUND_SCENARIOS), *closed],
    )


def is_background(scenario, job_id=None):
    return scenario in BACKGROUND_SCENARIOS and not (scenario == "memory_review" and job_id)


def capacity_exhausted(store, approval, closures=(), *, scenario="chat", job_id=None):
    counts = execution_counts(store, closures)
    return (counts["total"] >= approval.concurrency or is_background(scenario, job_id)
            and counts["background"] >= max(1, approval.concurrency - 1))


def capacity_wait(store, code, *, request_id=None):
    """Only an unadmitted or proven unsent attempt may wait, never an uncertain call."""
    return code in CAPACITY_WAIT_CODES and (request_id is None or not store.one(
        "SELECT 1 FROM model_usage WHERE request_id=?", (request_id,),
    ) or not_dispatched(store, request_id))


def not_dispatched(store, request_id):
    return bool(store.one(
        "SELECT 1 FROM model_usage m JOIN audit_log a ON a.entity_id=m.request_id "
        "WHERE m.request_id=? AND m.state='SETTLED' AND m.billed_micro_usd=0 "
        "AND m.input_tokens=0 AND m.output_tokens=0 AND m.is_estimate=0 "
        "AND a.action='model.not_dispatched' LIMIT 1", (request_id,),
    ))


def reader_video_binding(store, settings, clock, request_id, *, job_id=None, scenario=None,
                         logical_request_id=None):
    if not request_id or not store.one(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bili_video_phases'"
    ):
        return None
    from companion.bili_video import model_binding

    return model_binding(store, settings, clock, request_id, job_id=job_id, scenario=scenario,
                         logical_request_id=logical_request_id)


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


def reserve(
    store, settings, job_id, scenario, messages, now, *, request_id=None, logical_request_id=None,
    clock=None, cloud_task_kind=None, cloud_task_id=None, source_check=None, tools=(),
    generation=None,
):
    from companion import owner_authority

    native_image_chat = False
    cumulative.preflight(store, settings, clock or now)
    approval = paid_budget.contract(settings, now) if settings.mode == "live" else None
    video_binding = (reader_video_binding(store, settings, clock or (lambda: now), request_id,
        job_id=job_id, scenario=scenario, logical_request_id=logical_request_id or job_id)
        if scenario in VIDEO_SCENARIOS else None)
    if scenario == "content_video_preview" and video_binding is None:
        raise CoreError("BILI_VIDEO_MODEL_BINDING_REQUIRED", 403)
    if scenario == "meme_caption":
        from companion import meme_input

        logical_request_id = logical_request_id or job_id
        meme_input.contract(settings, job_id, logical_request_id)
    elif scenario in VIDEO_SCENARIOS:
        from companion.video_input import contract as video_contract

        logical_request_id = logical_request_id or job_id
        video_contract(settings, job_id, logical_request_id)
        if video_binding is not None:
            from companion.model import model_payload

            if (video_binding["payload_sha256"] != paid_budget.sha(
                model_payload(settings, messages, scenario, tools=tools))
                or cloud_task_kind not in {None, "video"}
                or cloud_task_id not in {None, video_binding["task_id"]}):
                raise CoreError("BILI_VIDEO_MODEL_BINDING_CHANGED", 403)
            cloud_task_kind, cloud_task_id = "video", video_binding["task_id"]
    elif scenario == "content_vision" or any(
        isinstance(message, dict) and isinstance(message.get("content"), list)
        for message in messages
    ):
        from companion.image_input import contract as image_contract

        logical_request_id = logical_request_id or job_id
        image_contract(settings, job_id, logical_request_id)
        if scenario == "chat" and owner_authority.required(store, settings):
            native_image_chat = True
        elif scenario != "content_vision" and (
            scenario != "chat" or not settings.live_text_window
            or settings.live_text_window.image_scope != "owner_image_v1"
        ):
            raise CoreError("MODEL_IMAGE_CHAT_JOB_REQUIRED", 503)
    autonomous = scenario not in {"chat", "qq_poke", "evaluation", "gateway"}
    if autonomous and not settings.pricing_known:
        raise CoreError("UNKNOWN_PRICING", 429)
    local = datetime.fromisoformat(now).astimezone(ZoneInfo(settings.timezone))
    month = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    measurement = measure_request(settings, messages, scenario, tools=tools, generation=generation)
    estimated_input = measurement.reserved_input_tokens
    output_tokens = measurement.output_allowance
    check_request_limits(approval, measurement, scenario)
    estimate = cost(settings, estimated_input, output_tokens, scenario=scenario)
    with store.transaction():
        from companion import live_text, owner_authority, runtime_config

        configuration = runtime_config.bind(store, settings, clock or (lambda: now),
                                           "job:" + job_id) if job_id is not None else None

        if job_id is not None:
            from companion.reply_revision import check_job

            job = store.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
            if job:
                check_job(store, job)
        if source_check is not None:
            source_check()
        if native_image_chat:
            from companion import agent_images

            if not job or job["kind"] != "chat":
                raise CoreError("MODEL_IMAGE_CHAT_JOB_REQUIRED", 503)
            owner_authority.check_job(store, settings, clock or (lambda: now), job,
                                      purpose="image", executing=True)
            agent_images.check_payload(store, settings, clock or (lambda: now), job, messages)

        if job_id is None and scenario in {"memory_extract", "memory_review"} and chat_pending(
            store, clock() if callable(clock) else now,
        ):
            raise CoreError("BACKGROUND_YIELD_TO_CHAT", 409)
        activate_cumulative = bool(approval and (approval.cumulative or store.one(
            "SELECT 1 FROM runtime_meta WHERE key=?", (cumulative.PROJECTION,),
        )))
        if activate_cumulative:
            now = clock() if callable(clock) else now
            local = datetime.fromisoformat(now).astimezone(ZoneInfo(settings.timezone))
            month = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
            day = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        if scenario == "meme_caption":
            meme_input.check_attempt(store, job_id)
        if video_binding is not None and reader_video_binding(
            store, settings, clock or (lambda: now), request_id, job_id=job_id,
            scenario=scenario, logical_request_id=logical_request_id,
        ) != video_binding:
            raise CoreError("BILI_VIDEO_MODEL_BINDING_CHANGED", 403)
        owner_view = None
        if scenario == "gateway":
            from companion import gateway

            gateway.admit(store, settings, clock or (lambda: now), request_id,
                          estimate, estimated_input)
        elif scenario == "evaluation":
            from companion import evaluation

            evaluation.admit(store, settings, clock, job_id, request_id, messages,
                             estimate, estimated_input)
        elif owner_authority.required(store, settings):
            owner_view = owner_authority.admit_model(
                store, settings, clock or (lambda: now), job_id, scenario)
        else:
            live_text.reserve_model(store, settings, clock, job_id, scenario)
        if scenario == "rem":
            from companion import rem

            rem.admit(store, settings, clock or (lambda: now), job_id,
                      estimated_input, estimate)
        request_id = request_id or uid("model")
        if store.one("SELECT 1 FROM model_usage WHERE request_id=?", (request_id,)):
            raise CoreError("MODEL_REQUEST_ALREADY_RESERVED", 409)
        if scenario == "social_autonomy":
            from companion.social_autonomy import SocialAutonomy

            if job_id is not None and source_check is None:
                raise CoreError("SOCIAL_MODEL_SCOPE_REQUIRED", 403)
            SocialAutonomy(store, settings, clock=clock or (lambda: now)).admit_model(
                request_id, logical_request_id, messages, job_id=job_id,
            )
        if scenario in {"content_vision", *VIDEO_SCENARIOS}:
            if not store.one("SELECT 1 FROM jobs WHERE job_id=? AND kind='chat'", (job_id,)):
                raise CoreError("MODEL_IMAGE_CHAT_JOB_REQUIRED", 503)
            if video_binding is None and store.one(
                "SELECT 1 FROM model_usage WHERE job_id=? AND scenario=?", (job_id, scenario),
            ):
                raise CoreError("MODEL_IMAGE_ALREADY_ATTEMPTED", 409)
        if scenario == "activity":
            from companion import notices
            from companion.social import proactive_allowed

            job = store.one("SELECT j.*,e.body_json FROM jobs j JOIN events e USING(event_id) "
                            "WHERE job_id=?", (job_id,))
            if notices.reference(job) is not None:
                notices.validate(store, settings, job, now)
            if (not notices.shadow(job) and not notices.required(store, job_id)
                and not proactive_allowed(store, settings, now)):
                raise CoreError("AUTONOMY_PAUSED", 409)
        total = total_monthly(store, month.isoformat(timespec="microseconds"))
        unlimited_period = bool(approval and approval.unlimited_period_spend)
        if (settings.pricing_known and not unlimited_period
            and total + estimate > settings.monthly_limit_micro_usd):
            raise CoreError("MONTHLY_BUDGET_EXHAUSTED", 429)
        if autonomous:
            if scenario in {"memory_extract", "memory_review"}:
                scenarios = ("memory_extract", "memory_review")
                limit = settings.memory_daily_micro_usd
            elif scenario == "rem":
                scenarios = ("rem",)
                limit = settings.rem.daily_micro_usd
            elif scenario == "style_distill":
                scenarios = ("style_distill",)
                limit = settings.style_distill.daily_micro_usd
                if not settings.style_distill.enabled or limit == 0:
                    raise CoreError("SCENARIO_BUDGET_DISABLED", 429)
            elif scenario == "jargon":
                scenarios = ("jargon",)
                limit = settings.jargon.daily_micro_usd
                if not settings.jargon.enabled or limit == 0:
                    raise CoreError("SCENARIO_BUDGET_DISABLED", 429)
            elif scenario == "watch_review":
                scenarios = ("watch_review",)
                limit = settings.watch_ledger.daily_micro_usd
                if not settings.watch_ledger.enabled or limit == 0:
                    raise CoreError("SCENARIO_BUDGET_DISABLED", 429)
            elif scenario == "diary":
                scenarios = ("diary",)
                limit = settings.diary_daily_micro_usd
            elif scenario in {"content_vision", *VIDEO_SCENARIOS}:
                scenarios = ("content_vision", *sorted(VIDEO_SCENARIOS))
                limit = settings.content.image_daily_micro_usd
            elif scenario == "meme_caption":
                scenarios = ("meme_caption",)
                limit = settings.meme_caption_daily_micro_usd
            elif scenario == "social_autonomy":
                scenarios = ("social_autonomy",)
                limit = settings.social_autonomy.model_daily_micro_usd
                calls = store.one("SELECT COUNT(*) n FROM model_usage "
                                  "WHERE scenario='social_autonomy' AND occurred_at>=?",
                                  (day.isoformat(timespec="microseconds"),))["n"]
                if not settings.social_autonomy.enabled or limit == 0:
                    raise CoreError("SCENARIO_BUDGET_DISABLED", 429)
                if calls >= settings.social_autonomy.model_daily_calls:
                    raise CoreError("SCENARIO_CALL_LIMIT", 429)
            elif scenario == "activity":
                scenarios = ("activity",)
                limit = settings.autonomous_daily_micro_usd
            elif scenario == "reflection":
                scenarios = ("reflection",)
                limit = settings.reflection_daily_micro_usd
                monthly = store.one(
                    "SELECT COALESCE(SUM(billed_micro_usd),0) AS n FROM model_usage "
                    "WHERE occurred_at>=? AND scenario='reflection'",
                    (month.isoformat(timespec="microseconds"),),
                )["n"]
                calls = store.one(
                    "SELECT COUNT(*) AS n FROM model_usage WHERE scenario='reflection' AND "
                    "occurred_at>=?",
                    (day.isoformat(timespec="microseconds"),),
                )["n"]
                if limit == 0 or settings.reflection_monthly_micro_usd == 0:
                    raise CoreError("SCENARIO_BUDGET_DISABLED", 429)
                if (not unlimited_period
                    and monthly + estimate > settings.reflection_monthly_micro_usd):
                    raise CoreError("SCENARIO_MONTHLY_BUDGET_EXHAUSTED", 429)
                if calls >= settings.reflection_max_daily_calls:
                    raise CoreError("SCENARIO_CALL_LIMIT", 429)
            else:
                raise CoreError("UNKNOWN_BUDGET_SCENARIO", 400)
            spent = store.one(
                "SELECT COALESCE(SUM(billed_micro_usd),0) AS n FROM model_usage "
                "WHERE occurred_at>=? AND scenario IN (" + ",".join("?" for _ in scenarios) + ")",
                (day.isoformat(timespec="microseconds"), *scenarios),
            )["n"]
            if (not unlimited_period or scenario in {"meme_caption", "social_autonomy"}) and (
                spent + estimate > limit
            ):
                raise CoreError("DAILY_BUDGET_EXHAUSTED", 429)
        if approval:
            logical_request_id = logical_request_id or job_id or uid("logical")
            _admit(store, settings, approval, logical_request_id, estimate, now, day, month,
                   scenario=scenario, job_id=job_id)
        cloud_pricing = (settings.paid_model.video_input if scenario in VIDEO_SCENARIOS
                         and settings.paid_model and settings.paid_model.video_input else settings)
        cloud_budget.reserve(
            store, settings, reference_type="model", reference_id=request_id,
            estimated_micro_usd=estimate, now=now,
            task_kind=cloud_task_kind or ("video" if scenario in VIDEO_SCENARIOS else "model"),
            task_id=cloud_task_id or logical_request_id or job_id or request_id,
            pricing={"pricing_known": bool(settings.pricing_known), **{
                key: getattr(cloud_pricing, key)
                for key in ("input_rate", "cached_input_rate", "output_rate")
            }},
        )
        if approval:
            # All monetary admission checks precede any journal initialization;
            # rejecting the CNY limit must not leave an orphaned fsynced header.
            if activate_cumulative:
                cumulative.activate(store, settings, now)
            paid_budget.journal(store, approval)
            quota = cumulative.admit_model(store, settings, request_id, logical_request_id, now,
                                            estimate, estimated_input, output_tokens)
        store.insert(
            "model_usage",
            usage_id=uid("usage"),
            request_id=request_id,
            job_id=job_id,
            scenario=scenario,
            provider=(approval.video_input.provider if scenario in VIDEO_SCENARIOS
                      else settings.provider),
            model=(approval.video_input.model if scenario in VIDEO_SCENARIOS
                   else settings.model_name),
            input_tokens=estimated_input,
            output_tokens=output_tokens,
            billed_micro_usd=estimate,
            is_estimate=1,
            pricing_known=int(settings.pricing_known),
            state="RESERVED",
            occurred_at=now,
        )
        if configuration is not None:
            runtime_config.bind(store, settings, clock or (lambda: now), "model:" + request_id,
                                snapshot=configuration)
        if approval:
            from companion.model import model_payload

            snapshot = paid_budget.snapshot(settings)
            contract_hash = paid_budget.sha(snapshot)
            store.insert(
                "model_admissions",
                request_id=request_id,
                logical_request_id=logical_request_id,
                attempt_id=uid("attempt"),
                attempt_number=1
                + store.one(
                    "SELECT COUNT(*) AS n FROM model_admissions WHERE logical_request_id=?",
                    (logical_request_id,),
                )["n"],
                window_id=approval.window_id,
                contract_sha256=contract_hash,
                payload_sha256=paid_budget.sha(model_payload(
                    settings, messages, scenario, tools=tools, generation=generation)),
                snapshot_json=encode(snapshot),
                measurement_json=encode(measurement.receipt()),
                reserved_micro_usd=estimate,
                input_bound=estimated_input,  # Historical SQL column; this is a local estimate.
                output_bound=output_tokens,
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
                        measurement.receipt())},
                    **({"quota": quota} if quota else {}),
                    **({"gateway": gateway.binding(store, request_id)}
                       if scenario == "gateway" else {}),
                },
            )
            from companion.private_life import freeze_model

            if owner_view is not None:
                store.execute("UPDATE model_admissions SET authority_json=? WHERE request_id=?",
                              (encode(owner_view.authority.as_dict()), request_id))
            elif scenario not in {"evaluation", "gateway"}:
                freeze_model(store, settings, request_id)
            elif scenario == "evaluation":
                from companion import owner_authority

                view = owner_authority.check_job(store, settings, clock,
                    store.one("SELECT * FROM jobs WHERE job_id=?", (job_id,)),
                    purpose="evaluation", executing=True)
                store.execute("UPDATE model_admissions SET authority_json=? WHERE request_id=?",
                              (encode(view.authority.as_dict()), request_id))
        return request_id


def _admit(store, settings, approval, logical, estimate, now, day, month, *, scenario="chat",
           job_id=None):
    from companion import model_carry

    carry_view = None
    if model_carry.active(settings):
        model_carry.supported(store, settings)
        carry_view = paid_budget.journal(store, approval)
        if store.one("SELECT 1 FROM model_admissions a JOIN model_usage m USING(request_id) "
                     "WHERE a.logical_request_id=? AND m.state='UNKNOWN' LIMIT 1", (logical,)):
            raise CoreError("MODEL_OUTCOME_UNKNOWN", 503)
    if store.one(
        "SELECT 1 FROM model_usage m LEFT JOIN model_admissions a USING(request_id) "
        "WHERE a.request_id IS NULL AND m.state IN ('RESERVED','UNKNOWN') LIMIT 1"
    ):
        raise CoreError("MODEL_LEGACY_USAGE_RECONCILIATION_REQUIRED", 503)
    if store.one(
        "SELECT 1 FROM runtime_meta WHERE key IN "
        "('model_budget_blocked','model_budget_restore_required')"
    ):
        raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)
    snapshot = paid_budget.snapshot(settings)
    frozen = paid_budget.sha(snapshot)
    prior = store.one(
        "SELECT contract_sha256 FROM model_budget_windows WHERE window_id=?", (approval.window_id,)
    )
    if prior and prior["contract_sha256"] != frozen:
        raise CoreError("PAID_WINDOW_CONTRACT_CHANGED", 503)
    store.execute(
        "INSERT OR IGNORE INTO model_budget_windows VALUES (?,?)", (approval.window_id, frozen)
    )
    if estimate <= 0 or estimate > approval.per_attempt_micro_usd:
        raise CoreError("MODEL_ATTEMPT_BUDGET_EXHAUSTED", 429)
    # Financial totals retain every liability; only verified execution closures
    # release capacity independently of the original accounting outcome.
    daily = store.one(
        "SELECT COUNT(*) AS calls,COALESCE(SUM(billed_micro_usd),0) AS amount "
        "FROM model_usage WHERE occurred_at>=?",
        (day.isoformat(timespec="microseconds"),),
    )
    if (
        daily["calls"] >= approval.daily_calls
        or (not approval.unlimited_period_spend
            and daily["amount"] + estimate > approval.daily_micro_usd)
    ):
        raise CoreError("MODEL_DAILY_BUDGET_EXHAUSTED", 429)
    if (
        not approval.unlimited_period_spend
        and total_monthly(store, month.isoformat(timespec="microseconds")) + estimate
        > approval.monthly_micro_usd
    ):
        raise CoreError("MODEL_MONTHLY_BUDGET_EXHAUSTED", 429)
    for column, value, amount, calls, code in (
        (
            "window_id",
            approval.window_id,
            approval.window_micro_usd,
            approval.window_calls,
            "WINDOW",
        ),
        (
            "logical_request_id",
            logical,
            approval.per_request_micro_usd,
            approval.per_request_calls,
            "REQUEST",
        ),
    ):
        window_scope = (model_carry.scope_windows(carry_view, value)
                        if column == "window_id" and carry_view else [value])
        usage = store.one(
            "SELECT COUNT(*) AS calls,COALESCE(SUM(m.billed_micro_usd),0) AS amount "
            "FROM model_admissions a JOIN model_usage m USING(request_id) WHERE a." + column
            + " IN (" + ",".join("?" for _ in window_scope) + ")", window_scope,
        )
        limited = column != "window_id" or not approval.unlimited_period_spend
        if usage["calls"] >= calls or (limited and usage["amount"] + estimate > amount):
            raise CoreError("MODEL_" + code + "_BUDGET_EXHAUSTED", 429)
    if capacity_exhausted(store, approval,
                          carry_view["model_closures"] if carry_view else (),
                          scenario=scenario, job_id=job_id):
        raise CoreError("MODEL_PROVIDER_CONCURRENCY_EXHAUSTED", 429)


def settle(store, settings, request_id, response=None, *, transport_failure=None):
    with nullcontext() if store.in_write_transaction else store.transaction():
        usage = store.one("SELECT occurred_at FROM model_usage WHERE request_id=?", (request_id,))
        if usage:
            cloud_budget.sync(store, settings, usage["occurred_at"])
        admission = store.one("SELECT * FROM model_admissions WHERE request_id=?", (request_id,))
        if admission:
            _settle_paid(store, settings, admission, response,
                         transport_failure=transport_failure)
            return
        if response is None or response.input_tokens is None or response.output_tokens is None:
            store.execute(
                "UPDATE model_usage SET state='UNKNOWN' WHERE request_id=?", (request_id,)
            )
        else:
            store.execute(
                "UPDATE model_usage SET input_tokens=?,output_tokens=?,"
                "cached_input_tokens=?,billed_micro_usd=?,state='SETTLED' "
                "WHERE request_id=?",
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
    if not store.db.in_transaction:
        raise RuntimeError("BUDGET_RECOVERY_TRANSACTION_REQUIRED")
    for admission in store.rows(
        "SELECT a.* FROM model_admissions a JOIN model_usage m USING(request_id) "
        "JOIN jobs j ON j.job_id=m.job_id WHERE j.state='CANCELLED' AND m.state='RESERVED' "
        "AND a.lease_until<=?", (now,),
    ):
        _settle_paid(store, settings, admission, None)


def _settle_unstarted_reply(store, request_id, dispatch, reason, now):
    """Record known zero provider I/O; keep the admitted call and its audit proof."""
    with store.transaction():
        admission = store.one("SELECT * FROM model_admissions WHERE request_id=?", (request_id,))
        usage = store.one("SELECT * FROM model_usage WHERE request_id=?", (request_id,))
        if (not admission or not usage or usage["state"] != "RESERVED" or dispatch["dispatched"]
            or (admission["io_started_at"] is not None and (
                not dispatch.get("owned")
                or admission["io_started_at"] != dispatch.get("io_started_at")))):
            return False
        evidence = paid_budget.sha({"kind": "reply_not_dispatched", "request_id": request_id,
            "lease_token": admission["lease_token"], "payload_sha256": admission["payload_sha256"],
            "reason": reason})
        _persist_accounting(store, admission, 0, "SETTLED", False, evidence,
                            tokens=(0, 0))
        store.execute("UPDATE model_usage SET input_tokens=0,output_tokens=0,"
                      "cached_input_tokens=0,is_estimate=0 WHERE request_id=? AND state='SETTLED'",
                      (request_id,))
        store.audit("model.not_dispatched", request_id, "budget", now,
                    reason=reason, evidence_sha256=evidence)
        return True


def _settle_paid(store, settings, admission, response, *, transport_failure=None):
    usage = store.one("SELECT * FROM model_usage WHERE request_id=?", (admission["request_id"],))
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
    rates = snapshot["video_input"] if usage["scenario"] in VIDEO_SCENARIOS else snapshot
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
        admission["reserved_micro_usd"], "UNKNOWN", response is not None and not valid,
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
        if snapshot.get("cumulative") or store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                                                  (cumulative.PROJECTION,)):
            try:
                view = paid_budget.journal(store, SimpleNamespace(
                    journal_path=admission["journal_path"]), allow_settlement=True)["cumulative"]
                prior = view["models"][admission["request_id"]]
                proposed = prior | {"micro_usd": actual, "state": "SETTLED",
                                   "input_tokens": response.input_tokens,
                                   "output_tokens": response.output_tokens}
                limits.extend("CUMULATIVE_" + code for code in cumulative.violations(
                    view, proposed, replacing="model:" + admission["request_id"]))
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
    if current_pricing is None or (
        current_pricing.input_rate, current_pricing.cached_input_rate, current_pricing.output_rate,
    ) != (pricing.input_rate, pricing.cached_input_rate, pricing.output_rate):
        amount, state, blocked = max(amount, admission["reserved_micro_usd"]), "UNKNOWN", True
        diagnostic["code"] = "PRICING_CONFIG_CHANGED"
    if usage["state"] == "UNKNOWN":
        amount, state = max(amount, usage["billed_micro_usd"]), "UNKNOWN"
    # Mutable enabling approval is deliberately irrelevant to accounting custody.
    _persist_accounting(store, admission, amount, state, blocked,
                        tokens=(response.input_tokens, response.output_tokens) if valid else None)
    if valid:
        store.execute(
            "UPDATE model_usage SET input_tokens=?,output_tokens=?,cached_input_tokens=? "
            "WHERE request_id=?",
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
        "model.usage_diagnostic", admission["request_id"], "budget", usage["occurred_at"],
        **diagnostic,
    )


def _actual_limit_violations(store, admission, snapshot, usage, actual):
    """Replace this reservation with known usage before comparing frozen local limits."""
    delta = actual - usage["billed_micro_usd"]
    local = datetime.fromisoformat(usage["occurred_at"]).astimezone(ZoneInfo(snapshot["timezone"]))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    next_day = (day + timedelta(days=1)).astimezone(UTC).isoformat(timespec="microseconds")
    next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    next_month = next_month.astimezone(UTC).isoformat(timespec="microseconds")
    day = day.astimezone(UTC).isoformat(timespec="microseconds")
    month = month.astimezone(UTC).isoformat(timespec="microseconds")
    limits = []
    if usage["scenario"] == "gateway":
        from companion.gateway import actual_limits

        limits.extend(actual_limits(store, usage, actual))
    if usage["scenario"] == "evaluation":
        from companion.evaluation import COST

        total = store.one("SELECT COALESCE(SUM(billed_micro_usd),0) AS n FROM model_usage "
                          "WHERE job_id=?", (usage["job_id"],))["n"]
        if actual > 5000 or total + delta > COST:
            limits.append("EVALUATION_AMOUNT")
    if actual > snapshot["per_attempt_micro_usd"]:
        limits.append("ATTEMPT_AMOUNT")
    for column, value, cap, code in (
        ("window_id", admission["window_id"], snapshot["window_micro_usd"], "WINDOW_AMOUNT"),
        ("logical_request_id", admission["logical_request_id"], snapshot["per_request_micro_usd"],
         "REQUEST_AMOUNT"),
    ):
        total = store.one(
            "SELECT SUM(m.billed_micro_usd) AS n FROM model_admissions a "
            "JOIN model_usage m USING(request_id) WHERE a." + column + "=?", (value,),
        )["n"]
        limited = column != "window_id" or not snapshot.get("unlimited_period_spend", False)
        if limited and total + delta > cap:
            limits.append(code)
    unlimited_period = snapshot.get("unlimited_period_spend", False)
    if unlimited_period and usage["scenario"] != "meme_caption":
        return limits
    if not unlimited_period:
        daily = store.one(
            "SELECT COALESCE(SUM(billed_micro_usd),0) AS n FROM model_usage "
            "WHERE occurred_at>=? AND occurred_at<?", (day, next_day),
        )["n"]
        if daily + delta > snapshot["daily_micro_usd"]:
            limits.append("DAILY_AMOUNT")
        monthly = total_monthly(store, month, next_month) + delta
        if monthly > min(snapshot["monthly_micro_usd"], snapshot.get(
            "local_monthly_micro_usd", snapshot["monthly_micro_usd"],
        )):
            limits.append("MONTHLY_AMOUNT")
    scenario = usage["scenario"]
    cap = snapshot.get("scenario_daily_micro_usd", {}).get(scenario)
    if cap is not None:
        scenarios = ("memory_extract", "memory_review") if scenario.startswith("memory_") else (
            scenario,
        )
        total = store.one(
            "SELECT COALESCE(SUM(billed_micro_usd),0) AS n FROM model_usage "
            "WHERE occurred_at>=? AND occurred_at<? AND scenario IN ("
            + ",".join("?" for _ in scenarios) + ")",
            (day, next_day, *scenarios),
        )["n"]
        if total + delta > cap:
            limits.append("SCENARIO_DAILY_AMOUNT")
    if scenario == "reflection" and "reflection_monthly_micro_usd" in snapshot:
        total = store.one(
            "SELECT COALESCE(SUM(billed_micro_usd),0) AS n FROM model_usage "
            "WHERE occurred_at>=? AND occurred_at<? AND scenario='reflection'",
            (month, next_month),
        )["n"]
        if total + delta > snapshot["reflection_monthly_micro_usd"]:
            limits.append("SCENARIO_MONTHLY_AMOUNT")
    return limits


def usage_diagnostics(db):
    """Read-only calibration from settled paid usage; no prompts or account identifiers."""
    cursor = db.execute(
        "SELECT COUNT(*) AS calibrated_calls,COALESCE(SUM(a.input_bound),0) AS "
        "input_estimated_tokens,COALESCE(SUM(m.input_tokens),0) AS input_actual_tokens,"
        "COALESCE(SUM(m.output_tokens),0) AS output_actual_tokens,"
        "COALESCE(SUM(m.cached_input_tokens),0) AS cached_input_tokens,"
        "COALESCE(SUM(a.reserved_micro_usd),0) AS reserved_micro_usd,"
        "COALESCE(SUM(m.billed_micro_usd),0) AS usage_priced_micro_usd "
        "FROM model_admissions a JOIN model_usage m USING(request_id) WHERE m.state='SETTLED' "
        # Read correction IDs once instead of scanning the audit log for every call.
        "AND m.request_id NOT IN (SELECT entity_id FROM audit_log "
        "WHERE action='model.video_rate_correction' AND entity_id IS NOT NULL)"
    )
    row = dict(zip((column[0] for column in cursor.description), cursor.fetchone(), strict=True))
    # Diagnostics never reinterpret the reserved tokens of an UNKNOWN as observed usage.
    anomalies = db.execute(
        "SELECT COUNT(DISTINCT entity_id) FROM audit_log "
        "WHERE action='model.usage_diagnostic' "
        "AND json_extract(safe_detail_json,'$.code') IN "
        "('INPUT_USAGE_SPIKE','LOCAL_LIMIT_EXCEEDED','USAGE_MISSING_OR_INVALID')"
    ).fetchone()[0]
    return {
        **row,
        "input_error_tokens": row["input_actual_tokens"] - row["input_estimated_tokens"],
        "cost_error_micro_usd": row["usage_priced_micro_usd"] - row["reserved_micro_usd"],
        "anomaly_calls": anomalies,
        "currency": "USD" if row["calibrated_calls"] else None,
        "billing_basis": "configured_rates_not_invoice",
    }


def _persist_accounting(
    store, admission, amount, state, blocked, evidence_sha256=None, *, tokens=None,
):
    """Record known costs even when approval is disabled or journal custody fails.

    No journal failure may roll back a known larger charge. The DB stop is committed;
    a damaged/missing independent journal additionally prevents future admission.
    """
    custody = SimpleNamespace(journal_path=admission["journal_path"])
    journal_failed = False
    quota = None
    try:
        identity = store.one("SELECT value FROM runtime_meta WHERE key='model_budget_journal_id'")
        if not identity or identity["value"] != admission["journal_id"]:
            raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)
        paid_budget.journal(store, custody, allow_settlement=True)
        quota = cumulative.admission_metadata(store, admission, amount=amount,
                                              state=state, tokens=tokens)
    except CoreError:
        journal_failed = True
    if journal_failed:
        amount, state, blocked = max(amount, admission["reserved_micro_usd"]), "UNKNOWN", True
    store.execute(
        "UPDATE model_usage SET billed_micro_usd=?,state=? WHERE request_id=?",
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
                "UPDATE model_usage SET billed_micro_usd=?,state=? WHERE request_id=?",
                (amount, state, admission["request_id"]),
            )
    if blocked:
        store.execute("INSERT OR REPLACE INTO runtime_meta VALUES ('model_budget_blocked','1')")
    cloud_budget.sync_reference(store, "model", admission["request_id"])


def _check_measurement(settings, admission, messages, scenario, *, tools=(), generation=None):
    measured = measure_request(settings, messages, scenario, tools=tools, generation=generation)
    check_request_limits(settings.paid_model, measured, scenario)
    if measured.reserved_input_tokens != admission["input_bound"]:
        raise CoreError("MODEL_INPUT_ESTIMATE_CHANGED", 503)
    saved = json.loads(admission["measurement_json"])
    if saved:
        if saved != measured.receipt() or saved["payload_sha256"] != admission["payload_sha256"]:
            raise CoreError("MODEL_TOKEN_MEASUREMENT_CHANGED", 503)
    elif settings.token_measurement is not None:
        # Empty migrated rows mean historical estimates; never upgrade their evidence.
        raise CoreError("MODEL_TOKEN_MEASUREMENT_CHANGED", 503)


async def complete(model, store, settings, request_id, messages, scenario, clock, *,
                   source_check=None, tools=(), trace_id=None, generation=None):
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
        response = await _dispatch(model, store, settings, request_id, messages, scenario, clock,
                                   dispatch=dispatch, source_check=source_check, tools=tools,
                                   trace_id=trace_id, generation=generation)
        return response
    except ModelResponseError as error:
        # An unusable reply can still carry observed usage.
        response = error.response
        raise
    except CoreError as error:
        if isinstance(error, ModelTransportError):
            transport_failure = error.failure
        known_not_sent = (isinstance(error, ModelDispatchRefused)
                          and error.admission is dispatch.get("admission"))
        before_io_yield = (error.code == "BACKGROUND_YIELD_TO_CHAT"
                           and not dispatch.get("owned"))
        if (isinstance(model, ModelClient) and (known_not_sent or before_io_yield
            or settings.mode == "live"
            and error.code in {CANCELLED, SOURCE_RECALLED, SUPERSEDED})):
            _settle_unstarted_reply(store, request_id, dispatch, error.code, clock())
        raise
    finally:
        # Cancellation is an unknown outcome too; callers never refund a started attempt.
        settle(store, settings, request_id, response, transport_failure=transport_failure)
        from companion import model_carry

        if model_carry.active(settings) and scenario != "gateway":
            model_carry.close_owned(store, settings, request_id, dispatch, clock())


def _model_scope(store, settings, clock, request_id, scenario, *, yield_to_chat=True):
    from companion import evaluation, live_text, owner_authority

    if scenario == "gateway":
        from companion import gateway

        return gateway.check_model(store, settings, clock, request_id)
    if scenario == "evaluation":
        return evaluation.check_request(store, settings, clock, request_id)
    if owner_authority.required(store, settings):
        return owner_authority.check_model(store, settings, clock, request_id,
                                           yield_to_chat=yield_to_chat)
    return live_text.check_model(store, settings, clock, request_id)


async def _dispatch(
    model, store, settings, request_id, messages, scenario, clock, *, dispatch=None,
    source_check=None, tools=(), trace_id=None, generation=None,
):
    """Persisted admission is fenced before adapter I/O."""
    from companion import live_text, notices, owner_authority, runtime_config
    from companion.telemetry import phase

    if source_check is not None:
        with phase("dispatch_sources", trace_id):
            source_check()
    with phase("dispatch_scope", trace_id):
        runtime_config.check_request(store, settings, request_id)
        _model_scope(store, settings, clock, request_id, scenario)
        notices.check_request(store, settings, request_id, clock() if callable(clock) else clock)
    if settings.mode != "live":
        with store.transaction():
            cloud_budget.mark_dispatched(store, "model", request_id,
                                         clock() if callable(clock) else clock)
        return await model.complete(messages, scenario, **({"tools": tools} if tools else {}))
    if not callable(clock):
        raise CoreError("MODEL_ADMISSION_CLOCK_REQUIRED", 503)
    cumulative.preflight(store, settings, clock)
    now = clock()
    from companion.model import ModelAdmission, ModelClient, model_payload

    if (owner_authority.required(store, settings) or live_text.active(store, settings)
        or scenario in {"content_vision", *VIDEO_SCENARIOS, "meme_caption", "gateway"}):
        if not isinstance(model, ModelClient):
            raise CoreError("LIVE_TEXT_MODEL_ADAPTER_REQUIRED", 503)
        target_url = (settings.paid_model.video_input.endpoint if scenario in VIDEO_SCENARIOS
                      else settings.model_url)
        if live_text.active(store, settings) and (
            settings.live_text_window.execution == "local_synthetic"
        ) and not isinstance(
            model.client._transport_for_url(httpx.URL(target_url)), httpx.MockTransport
        ):
            raise CoreError("LIVE_TEXT_SYNTHETIC_MODEL_TRANSPORT_REQUIRED", 503)
    if isinstance(model, ModelClient) and model.settings is not settings:
        raise CoreError("MODEL_ADMISSION_CONFIGURATION_MISMATCH", 503)
    approval = paid_budget.contract(settings, now)
    with phase("dispatch_journal", trace_id), store.transaction():
        cumulative.preflight(store, settings, clock)
        paid_budget.contract(settings, clock())
        custody = paid_budget.journal(store, approval)
        cumulative.check(store, settings, clock(), custody=custody)
        row = store.one(
            "SELECT a.*,m.state,m.scenario AS usage_scenario,m.job_id AS usage_job_id "
            "FROM model_admissions a JOIN model_usage m USING(request_id) "
            "WHERE request_id=?",
            (request_id,),
        )
        if not row or row["io_started_at"] or row["state"] != "RESERVED":
            raise CoreError("MODEL_REQUEST_ALREADY_STARTED", 409)
        if row["usage_scenario"] != scenario:
            raise CoreError("MODEL_ADMISSION_PAYLOAD_CHANGED", 503)
        if scenario == "meme_caption":
            from companion import meme_input

            meme_input.contract(settings, row["usage_job_id"], row["logical_request_id"])
            meme_input.check_attempt(store, row["usage_job_id"], request_id)
        elif scenario in {"content_vision", *VIDEO_SCENARIOS}:
            from companion.image_input import contract as image_contract
            from companion.video_input import contract as video_contract

            reader_binding = reader_video_binding(store, settings, clock, request_id,
                job_id=row["usage_job_id"], scenario=scenario,
                logical_request_id=row["logical_request_id"])
            if scenario == "content_video_preview" and reader_binding is None:
                raise CoreError("BILI_VIDEO_MODEL_BINDING_REQUIRED", 403)
            if reader_binding is None:
                image_contract(settings, row["usage_job_id"], row["logical_request_id"])
            if scenario in VIDEO_SCENARIOS:
                video_contract(settings, row["usage_job_id"], row["logical_request_id"])
        if datetime.fromisoformat(row["lease_until"]) <= datetime.fromisoformat(now):
            raise CoreError("MODEL_ADMISSION_LEASE_EXPIRED", 503)
        snapshot = paid_budget.snapshot(settings)
        if row["contract_sha256"] != paid_budget.sha(snapshot):
            raise CoreError("PAID_WINDOW_CONTRACT_CHANGED", 503)
        if row["payload_sha256"] != paid_budget.sha(model_payload(
            settings, messages, scenario, tools=tools, generation=generation)):
            raise CoreError("MODEL_ADMISSION_PAYLOAD_CHANGED", 503)
        _check_measurement(settings, row, messages, scenario, tools=tools, generation=generation)
        if store.one(
            "SELECT 1 FROM runtime_meta WHERE key IN "
            "('model_budget_blocked','model_budget_restore_required')"
        ):
            raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)
        quota = cumulative.admission_metadata(store, row)
        if quota:
            now = clock()
            cumulative.check(store, settings, now)
            if datetime.fromisoformat(row["lease_until"]) <= datetime.fromisoformat(now):
                raise CoreError("MODEL_ADMISSION_LEASE_EXPIRED", 503)
        store.execute(
            "UPDATE model_admissions SET io_started_at=? WHERE request_id=?", (now, request_id)
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
    if dispatch is not None:
        dispatch.update(owned=True, io_started_at=now)
        from companion import model_carry

        if model_carry.active(settings) and scenario != "gateway":
            dispatch["owner_tuple"] = model_carry._tuple({**row, "io_started_at": now})
    # Re-read after lock acquisition, input estimation and journal fsync; no stale
    # pre-lock timestamp may authorize an HTTP request after the deadline.
    with phase("dispatch_recheck", trace_id):
        cumulative.preflight(store, settings, clock)
        paid_budget.contract(settings, clock())
        # I/O custody already began and this phase cannot prove non-dispatch, so a late
        # owner message is left to the HTTP-boundary check, which can.
        _model_scope(store, settings, clock, request_id, scenario, yield_to_chat=False)
        if row["contract_sha256"] != paid_budget.sha(paid_budget.snapshot(settings)):
            raise CoreError("PAID_WINDOW_CONTRACT_CHANGED", 503)
        if datetime.fromisoformat(row["lease_until"]) <= datetime.fromisoformat(clock()):
            raise CoreError("MODEL_ADMISSION_LEASE_EXPIRED", 503)
    if isinstance(model, ModelClient):
        async def before_dispatch(request):
            # Every user hook has completed. Custody and current runtime checks
            # remain here; the adapter only binds the actual HTTP request bytes.
            with phase("transport_custody", trace_id), store.transaction():
                runtime_config.check_request(store, settings, request_id)
                custody = paid_budget.journal(store, approval)
                quota_state = cumulative.check(store, settings, clock(), custody=custody)
                current_job = _model_scope(store, settings, clock, request_id, scenario)
                notices.check_request(store, settings, request_id, clock())
                current = store.one(
                    "SELECT a.*,m.state,m.scenario,m.job_id AS usage_job_id "
                    "FROM model_admissions a JOIN model_usage m "
                    "USING(request_id) WHERE request_id=?", (request_id,),
                )
                if (
                    not current or current["state"] != "RESERVED"
                    or current["lease_token"] != row["lease_token"]
                    or current["io_started_at"] != now
                    or current["payload_sha256"] != row["payload_sha256"]
                    or current["contract_sha256"] != row["contract_sha256"]
                    or current["measurement_json"] != row["measurement_json"]
                    or (scenario in {"content_vision", *VIDEO_SCENARIOS, "meme_caption"} and (
                        current["scenario"] != scenario
                        or current["usage_job_id"] != row["usage_job_id"]
                        or current["logical_request_id"] != row["logical_request_id"]
                    ))
                ):
                    raise CoreError("MODEL_ADMISSION_LEASE_EXPIRED", 503)
                if store.one("SELECT 1 FROM runtime_meta WHERE key IN "
                             "('model_budget_blocked','model_budget_restore_required')"):
                    raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)
            paid_budget.contract(settings, clock())
            if scenario == "meme_caption":
                meme_input.contract(settings, row["usage_job_id"], row["logical_request_id"])
                meme_input.check_attempt(store, row["usage_job_id"], request_id)
            elif scenario in {"content_vision", *VIDEO_SCENARIOS}:
                reader_binding = reader_video_binding(store, settings, clock, request_id,
                    job_id=row["usage_job_id"], scenario=scenario,
                    logical_request_id=row["logical_request_id"])
                if scenario == "content_video_preview" and reader_binding is None:
                    raise CoreError("BILI_VIDEO_MODEL_BINDING_REQUIRED", 403)
                if reader_binding is None:
                    image_contract(settings, row["usage_job_id"], row["logical_request_id"])
                if scenario in VIDEO_SCENARIOS:
                    video_contract(settings, row["usage_job_id"], row["logical_request_id"])
            with phase("transport_payload", trace_id):
                if row["contract_sha256"] != paid_budget.sha(paid_budget.snapshot(settings)):
                    raise CoreError("PAID_WINDOW_CONTRACT_CHANGED", 503)
                if row["payload_sha256"] != paid_budget.sha(
                    model_payload(settings, messages, scenario, tools=tools, generation=generation),
                ):
                    raise CoreError("MODEL_ADMISSION_PAYLOAD_CHANGED", 503)
                _check_measurement(settings, current, messages, scenario, tools=tools,
                                   generation=generation)
            if source_check is not None:
                # Caller-owned evidence may expire while an HTTP hook awaits.
                # Keep this synchronous proof inside the existing transport fence.
                with phase("transport_sources", trace_id), store.transaction():
                    source_check()
            with store.transaction():
                cloud_budget.mark_dispatched(store, "model", request_id, clock())
            if (scenario != "gateway" and live_text.active(store, settings)
                and not owner_authority.required(store, settings)):
                if settings.live_text_window.execution == "local_synthetic" and not isinstance(
                    model.client._transport_for_url(request.url), httpx.MockTransport
                ):
                    raise CoreError("LIVE_TEXT_SYNTHETIC_MODEL_TRANSPORT_REQUIRED", 503)
                live_text.dispatch_fence(
                    settings, clock, current_job["lease_until"], current["lease_until"],
                    quota_state=quota_state, request_id=request_id,
                )
            else:
                paid = paid_budget.contract(settings, clock())
                lease_deadline = datetime.fromisoformat(current["lease_until"])
                if scenario == "gateway" or owner_authority.required(store, settings):
                    # Budget synchronization can race a separate owner's STOP.
                    # Re-read its native authority/consent before the final clock sample.
                    current_job = _model_scope(store, settings, clock, request_id, scenario)
                # All preceding checks are complete. One final sample must fence
                # every time limit, not only the longer admission lease.
                dispatch_now = datetime.fromisoformat(clock())
                if not paid_budget.active(paid, dispatch_now):
                    raise CoreError("PAID_APPROVAL_EXPIRED", 503)
                if not paid_budget.rates_valid(paid, dispatch_now):
                    raise CoreError("PAID_RATES_EXPIRED", 503)
                if dispatch_now >= lease_deadline:
                    raise CoreError("MODEL_ADMISSION_LEASE_EXPIRED", 503)
                if current_job and dispatch_now >= datetime.fromisoformat(
                    current_job["lease_until"]
                ):
                    raise CoreError("EVALUATION_JOB_FENCED" if scenario == "evaluation"
                                    else "JOB_LEASE_LOST", 409)
                cumulative.final_fence(settings, quota_state, dispatch_now, request_id)
            if dispatch is not None:
                # The checked model transport calls its delegate immediately after
                # this hook; no await separates the final proof from that handoff.
                dispatch["dispatched"] = True
        admission = ModelAdmission(row["payload_sha256"], before_dispatch, trace_id=trace_id)
        if dispatch is not None:
            dispatch["admission"] = admission
    if isinstance(model, ModelClient):
        try:
            return await model.complete(messages, scenario, admission=admission,
                                        **({"tools": tools} if tools else {}),
                                        **({"generation": generation} if generation else {}))
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
            "SELECT a.*,m.billed_micro_usd FROM model_admissions a "
            "JOIN model_usage m USING(request_id) WHERE request_id=?",
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
