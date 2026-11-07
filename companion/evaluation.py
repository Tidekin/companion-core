"""Finite, owner-authorized experiments on the original Core job/budget authority.

The projections below own frozen inputs and erasable observations, never a wallet,
provider transport, delivery channel or second task executor.
"""

import json
from contextlib import nullcontext
from datetime import datetime
from typing import Literal

import httpx
from pydantic import Field, model_validator

from companion import input_recall, job_queue, owner_authority, paid_budget, reply_revision
from companion.contracts import CoreError, StrictModel
from companion.evaluation_measurement import (
    measurement_profile as _measurement_profile,
)
from companion.evaluation_measurement import (
    request_measurement as _request_measurement,
)
from companion.model import ModelClient, model_payload
from companion.observation import expired_sources
from companion.policy import ConsentService
from companion.privacy import epoch, redact
from companion.private_life import event_digest
from companion.store import after, encode

CAPABILITY = "model.evaluate"
DEVICE = "core.evaluation"
CALLS = 6
COST = 50000
ATTEMPT_MICRO_USD = 5000
SHA = r"^[a-f0-9]{64}$"
# A pilot replays owner turns; a synthetic plan carries only prepared SYNTHETIC cases.
LIMITS = {
    "owner_pilot": {"calls": CALLS, "cost": COST, "cases": 1, "bytes": 120000, "input": 2400},
    "synthetic": {"calls": 216, "cost": 1000000, "cases": 36, "bytes": 1500000, "input": 8000},
}
MAX_HTTP_BODY_BYTES = max(value["bytes"] for value in LIMITS.values()) + 1024
# Requests per worker claim; later batches queue behind ordinary chat.
BATCH = 6
# Accepted treatments and the arm each is compared against.
CONTROLS = {"participation_v2": "baseline", "participation_v2_bili_style": "participation_v2"}
IMAGE_TREATMENTS = {"image_understanding": "baseline", "image_persona": "image_understanding"}
CONTROLS.update(IMAGE_TREATMENTS)


class Manifest(StrictModel):
    plan: dict
    kind: Literal["owner_pilot", "synthetic"] = Field(
        default="owner_pilot", exclude_if=lambda value: value == "owner_pilot",
    )
    source_event_ids: list[str] = Field(default_factory=list, max_length=12)
    baseline_sha256: str = Field(pattern=SHA)
    source_document_sha256: str = Field(pattern=SHA)

    @model_validator(mode="after")
    def scoped_sources(self):
        if (self.kind == "synthetic") != (not self.source_event_ids):
            raise ValueError("owner pilots need sources; synthetic plans carry none")
        return self


def _limits(manifest):
    return LIMITS[manifest.get("kind", "owner_pilot")]


class Submission(StrictModel):
    manifest: Manifest
    consent_id: str = Field(min_length=1, max_length=96)


def request_id(manifest_hash, ordinal):
    return "model_eval_" + paid_budget.sha([manifest_hash, ordinal])


def _enabled(settings):
    if not settings.evaluation_enabled:
        raise CoreError("EVALUATION_DISABLED", 403)
    if settings.mode != "live":
        raise CoreError("EVALUATION_PAID_ADMISSION_REQUIRED", 403)


def _plan(settings, manifest):
    """Validate the prepared plan without rewriting a single provider field."""
    try:
        plan, limits = manifest["plan"], _limits(manifest)
        synthetic = manifest.get("kind", "owner_pilot") == "synthetic"
        if len(encode(manifest).encode()) > limits["bytes"] or redact(
            encode(manifest), settings
        ) != encode(manifest):
            raise ValueError("private input size or credentials")
        if paid_budget.sha({k: v for k, v in plan.items() if k != "plan_sha256"}) != (
            plan["plan_sha256"]
        ):
            raise ValueError("plan digest")
        version = plan.get("schema_version", 1)
        if type(version) is not int or version not in {1, 2}:
            raise ValueError("plan version")
        if version == 2 and plan.get("measurement_profile") != _measurement_profile(settings):
            raise ValueError("measurement policy, source or route changed")
        if synthetic and version != 2:
            raise ValueError("synthetic plans need native measurement")
        # v1 plans carry no treatment field, so their digests are unchanged.
        treatment = plan.get("treatment", "participation_v1")
        if "treatment" in plan and treatment not in CONTROLS:
            raise ValueError("treatment")
        # The bili-style arm is compared with v2 itself; older plans have no control key.
        control = plan.get("control", "baseline")
        if control != CONTROLS.get(treatment, "baseline"):
            raise ValueError("treatment control")
        rows = plan["requests"]
        image_trial = synthetic and version == 2 and treatment in IMAGE_TREATMENTS
        repetitions = plan.get("repetitions", 3) if synthetic else 3
        cases = {row["case"] for row in rows}
        if (type(repetitions) is not int or not 1 <= repetitions <= 3
            or plan["request_count"] != len(rows) or plan["case_count"] != len(cases)
            or not 1 <= len(cases) <= limits["cases"] or len(rows) > limits["calls"]
            or (not synthetic and len(rows) != CALLS)
            or plan["actual_provider_calls"] != 0 or plan["actual_cost_micro_usd"] != 0
            or plan["status"] != "PREPARED_NO_MODEL_CALLS"
            or (plan["provider"], plan["model"]) != (settings.provider, settings.model_name)):
            raise ValueError("plan scope")
        pairs = {(r, s) for r in range(repetitions) for s in (control, treatment)}
        if any({(row["repetition"], row["strategy"]) for row in rows if row["case"] == case}
               != pairs for case in cases) or len(rows) != len(cases) * len(pairs):
            raise ValueError("pairs")
        rates = plan["rates_micro_usd_per_million"]
        if (rates["input"], rates["cached_input"], rates["output"]) != (
            settings.input_rate, settings.cached_input_rate, settings.output_rate
        ):
            raise ValueError("configured rates changed")
        shared = {}
        measured_costs = []
        for row in rows:
            messages = row["payload"]["messages"]
            if (not 2 <= len(messages) <= 60 or messages[0]["role"] != "system"
                or messages[-1]["role"] != "user" or any(
                    set(m) != {"role", "content"} or not (
                        isinstance(m["content"], str) or image_trial and m["role"] == "user"
                        and isinstance(m["content"], list))
                    or m["role"] not in ({"system"} if i == 0 else {"user", "assistant"})
                    for i, m in enumerate(messages))):
                raise ValueError("text-only messages")
            if any(isinstance(m["content"], list) for m in messages):
                from companion.image_input import messages_and_estimate

                # The original paid image contract validates inline bytes, image
                # count, MIME, decoder dimensions and the separate token estimate.
                messages_and_estimate(settings.paid_model.image_input, messages)
            payload = model_payload(settings, messages, "evaluation")
            digest = paid_budget.sha(messages[1:])
            if (payload != row["payload"] or paid_budget.sha(payload) != row["payload_sha256"]
                or row["shared_context_sha256"] != digest
                or row["planned_max_output_tokens"] != settings.max_output_tokens
                or settings.max_output_tokens > 2048):
                raise ValueError("fixed payload")
            if version == 2:
                measured = _request_measurement(settings, payload)
                if measured["budget_input_tokens"] is None or any(
                    row.get(key) != value for key, value in measured.items()
                ) or measured["budget_input_tokens"] > limits["input"] or measured[
                    "estimated_reservation_micro_usd"] > ATTEMPT_MICRO_USD:
                    raise ValueError("unsupported, changed or over-limit measurement")
                measured_costs.append(measured["estimated_reservation_micro_usd"])
            shared.setdefault(row["case"], set()).add(digest)
        if version == 2 and (plan.get("repetitions") != repetitions
                             or plan.get("estimated_reservation_micro_usd") != sum(measured_costs)
                             or sum(measured_costs) > limits["cost"]):
            raise ValueError("measurement total or repetition count")
        if any(len(digests) != 1 for digests in shared.values()) or len(
            set(manifest["source_event_ids"])) != len(manifest["source_event_ids"]):
            raise ValueError("shared evidence")
        return rows
    except (KeyError, TypeError, ValueError, AttributeError):
        raise CoreError("EVALUATION_MANIFEST_INVALID", 400) from None


def _sources(store, settings, ids, now):
    result, conversations = {}, set()
    for source_id in ids:
        source = store.one('SELECT e.event_id AS event_id, e.schema_version AS schema_version, '
            'e.source AS source, e.source_event_id AS source_event_id, e.conversation_id AS '
            'conversation_id, e.actor_id AS actor_id, e.event_type AS event_type, e.occurred_at AS '
            'occurred_at, e.ingested_at AS ingested_at, e.privacy_scope AS privacy_scope, '
            'e.body_json AS body_json, e.trace_id AS trace_id, e.idempotency_key AS '
            'idempotency_key, e.fingerprint AS fingerprint, e.redacted AS redacted, '
            'e.memory_processed AS memory_processed, e.reply_revision AS reply_revision, c.scope '
            'AS scope, c.owner_actor_id AS owner_actor_id FROM events AS e JOIN conversations AS c '
            'ON e.conversation_id = c.conversation_id WHERE e.event_id = %s', (source_id,))
        if (not source or not reply_revision.owner_input(store, source_id)
            or not reply_revision.source_active(store, source_id)
            or source["scope"] != "private" or source["owner_actor_id"] != "owner"):
            raise CoreError("EVALUATION_OWNER_SOURCE_REQUIRED", 403)
        input_recall.require_not_recalled(store, source["source_event_id"])
        conversations.add(source["conversation_id"])
        sent = store.rows('SELECT message_id, content_sha256, sent_at FROM outbox WHERE '
            "origin_event_id = %s AND state = 'SENT' ORDER BY message_id NULLS FIRST",
                          (source_id,))
        result[source_id] = {"event_sha256": event_digest(source), "sent": sent}
    if len(conversations) != 1 or expired_sources(
        store, ids, after(now, -86400 * settings.raw_event_retention_days)
    ):
        raise CoreError("EVALUATION_SOURCE_EXPIRED_OR_MIXED", 409)
    return result, conversations.pop()


def _authority(store, settings, clock, current_request=None):
    _enabled(settings)
    paid_budget.contract(settings, clock())
    view = owner_authority.current(store, settings, clock, purpose="evaluation")
    return {"paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(settings)),
            "authority": view.authority.as_dict(),
            "privacy_epoch": epoch(store)}


def _conversation_binding(store, conversation):
    latest = store.one('SELECT event_id FROM events WHERE conversation_id=%s AND '
                       + reply_revision.OWNER_INPUT + ' ORDER BY record_sequence DESC LIMIT 1',
                       (conversation,))
    return {"conversation_revision": store.one(
        'SELECT reply_revision FROM conversations WHERE conversation_id = %s',
        (conversation,))["reply_revision"], "latest_owner_event": latest["event_id"]}


def _inputs(store, settings, manifest, now):
    """Owner sources and conversation fences; a synthetic plan has neither."""
    if manifest.get("kind", "owner_pilot") == "synthetic":
        row = store.one("SELECT conversation_id FROM conversations WHERE scope = 'private' AND "
            "owner_actor_id = 'owner' ORDER BY record_sequence NULLS FIRST LIMIT 1")
        if not row:
            raise CoreError("EVALUATION_OWNER_SOURCE_REQUIRED", 403)
        return {}, row["conversation_id"], {}
    sources, conversation = _sources(store, settings, manifest["source_event_ids"], now)
    return sources, conversation, _conversation_binding(store, conversation)


def preview(store, settings, clock, manifest):
    _enabled(settings)
    data = manifest.model_dump()
    rows = _plan(settings, data)
    _inputs(store, settings, data, clock())
    _authority(store, settings, clock)
    digest = paid_budget.sha(data)
    return {"manifest_sha256": digest, "request_count": len(rows),
            "max_cost_micro_usd": _limits(data)["cost"], "consent_scope": {
                "capabilities": [CAPABILITY], "device_id": DEVICE, "session_id": digest}}


def submit(core, request):
    store, settings, clock = core.store, core.settings, core.clock
    with store.transaction():
        prepared = preview(store, settings, clock, request.manifest)
        digest, now = prepared["manifest_sha256"], clock()
        consent = ConsentService(store, settings, clock).require(
            request.consent_id, CAPABILITY, DEVICE, digest)
        previous = store.one('SELECT evaluation_runs.evaluation_id AS evaluation_id, '
            'evaluation_runs.manifest_sha256 AS manifest_sha256, evaluation_runs.consent_id AS '
            'consent_id, evaluation_runs.job_id AS job_id, evaluation_runs.manifest_json AS '
            'manifest_json, evaluation_runs.sources_json AS sources_json, '
            'evaluation_runs.binding_json AS binding_json, evaluation_runs.redacted AS redacted, '
            'evaluation_runs.created_at AS created_at FROM evaluation_runs AS evaluation_runs '
            'WHERE evaluation_runs.manifest_sha256 = %s', (digest,))
        if previous:
            return {"evaluation_id": previous["evaluation_id"], "job_id": previous["job_id"],
                    "manifest_sha256": digest, "reused": True}
        manifest = request.manifest.model_dump()
        sources, conversation, fences = _inputs(store, settings, manifest, now)
        binding = _authority(store, settings, clock)
        binding.update(fences)
        evaluation_id, event_id = "eval_" + digest, "evt_eval_" + digest
        store.insert("events", event_id=event_id, schema_version=1, source="system.control",
                     source_event_id=evaluation_id, conversation_id=conversation, actor_id="system",
                     event_type="evaluation.requested", occurred_at=now, ingested_at=now,
                     privacy_scope="private", body_json=encode({"evaluation_id": evaluation_id}),
                     trace_id=evaluation_id, memory_processed=1)
        job_id = core.new_job(event_id, "evaluation", evaluation_id, now, max_attempts=1)
        owner_authority.bind_job(store, settings, clock, job_id, purpose="evaluation")
        for source_id in sources:
            store.insert("event_dependencies", event_id=event_id, source_event_id=source_id)
            store.insert("job_sources", job_id=job_id, event_id=source_id)
        store.insert("evaluation_runs", evaluation_id=evaluation_id, manifest_sha256=digest,
                     consent_id=consent["consent_id"], job_id=job_id,
                     manifest_json=encode(manifest),
                     sources_json=encode(sources), binding_json=encode(binding), created_at=now)
        store.audit("evaluation.submitted", evaluation_id, evaluation_id, now,
                    manifest_sha256=digest, request_count=prepared["request_count"])
        return {"evaluation_id": evaluation_id, "job_id": job_id,
                "manifest_sha256": digest, "reused": False}


def check_job(store, settings, clock, job_id, *, lease_token=None, current_request=None):
    run = store.one('SELECT r.evaluation_id AS evaluation_id, r.manifest_sha256 AS '
        'manifest_sha256, r.consent_id AS consent_id, r.job_id AS job_id, r.manifest_json AS '
        'manifest_json, r.sources_json AS sources_json, r.binding_json AS binding_json, r.redacted '
        'AS redacted, r.created_at AS created_at, j.state AS state, j.lease_token AS lease_token, '
        'j.lease_until AS lease_until, j.event_id AS event_id, e.conversation_id AS '
        'conversation_id FROM evaluation_runs AS r JOIN jobs AS j ON r.job_id = j.job_id JOIN '
        'events AS e ON e.event_id = j.event_id WHERE r.job_id = %s AND j.kind = '
        "'evaluation'", (job_id,))
    if not run or run["redacted"] or run["state"] != "RUNNING" or (
        not run["lease_until"] or run["lease_until"] <= clock()
    ) or lease_token is not None and lease_token != run["lease_token"]:
        raise CoreError("EVALUATION_JOB_FENCED", 409)
    owner_authority.check_job(store, settings, clock, run, purpose="evaluation", executing=True)
    reply_revision.check_sources(store, job_id)
    ConsentService(store, settings, clock).require(
        run["consent_id"], CAPABILITY, DEVICE, run["manifest_sha256"])
    manifest = json.loads(run["manifest_json"])
    if paid_budget.sha(manifest) != run["manifest_sha256"]:
        raise CoreError("EVALUATION_MANIFEST_CHANGED", 409)
    _plan(settings, manifest)
    current, _, fences = _inputs(store, settings, manifest, clock())
    binding = _authority(store, settings, clock, current_request)
    binding.update(fences)
    if current != json.loads(run["sources_json"]) or binding != json.loads(run["binding_json"]):
        raise CoreError("EVALUATION_SOURCE_OR_AUTHORITY_CHANGED", 409)
    return run, manifest


def admit(store, settings, clock, job_id, rid, messages, estimate, estimated_input):
    if not store.in_transaction:
        raise RuntimeError("EVALUATION_ADMISSION_TRANSACTION_REQUIRED")
    run, manifest = check_job(store, settings, clock, job_id)
    prior = store.rows('SELECT request_id, state, billed_micro_usd FROM model_usage WHERE job_id = '
        '%s ORDER BY record_sequence NULLS FIRST', (job_id,))
    limits = _limits(manifest)
    if (len(prior) >= min(limits["calls"], len(manifest["plan"]["requests"]))
        or any(row["state"] != "SETTLED" for row in prior)
        or sum(row["billed_micro_usd"] for row in prior) + estimate > limits["cost"]
        or estimate > ATTEMPT_MICRO_USD or estimated_input > limits["input"]):
        raise CoreError("EVALUATION_SUBLIMIT_EXHAUSTED", 429)
    ordinal = len(prior)
    if (rid != request_id(run["manifest_sha256"], ordinal)
        or any(row["request_id"] != request_id(run["manifest_sha256"], i)
               for i, row in enumerate(prior))
        or len(store.rows('SELECT 1 FROM evaluation_results WHERE evaluation_id = %s',
                          (run["evaluation_id"],))) != ordinal
        or paid_budget.sha(model_payload(settings, messages, "evaluation")) != (
            manifest["plan"]["requests"][ordinal]["payload_sha256"])):
        raise CoreError("EVALUATION_SEQUENCE_OR_PAYLOAD_CHANGED", 409)


def actual_limits(store, usage, actual):
    total = store.one((
        'SELECT COALESCE(SUM(billed_micro_usd),0) AS n FROM model_usage WHERE '
        'job_id=%s'
    ), (usage["job_id"],))["n"]
    if actual > ATTEMPT_MICRO_USD or total + actual - usage["billed_micro_usd"] > COST:
        return ["EVALUATION_AMOUNT"]
    return []


def check_request(store, settings, clock, rid):
    usage = store.one('SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS '
        'request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, '
        'model_usage.provider AS provider, model_usage.model AS model, model_usage.input_tokens AS '
        'input_tokens, model_usage.cached_input_tokens AS cached_input_tokens, '
        'model_usage.output_tokens AS output_tokens, model_usage.billed_micro_usd AS '
        'billed_micro_usd, model_usage.is_estimate AS is_estimate, model_usage.occurred_at AS '
        'occurred_at, model_usage.state AS state, model_usage.pricing_known AS pricing_known FROM '
        'model_usage AS model_usage WHERE model_usage.request_id = %s AND model_usage.scenario = '
        "'evaluation'",
                      (rid,))
    if not usage or usage["state"] != "RESERVED":
        raise CoreError("EVALUATION_REQUEST_FENCED", 409)
    run, manifest = check_job(store, settings, clock, usage["job_id"], current_request=rid)
    matching = [i for i in range(len(manifest["plan"]["requests"]))
                if rid == request_id(run["manifest_sha256"], i)]
    admission = store.one('SELECT payload_sha256, authority_json FROM model_admissions WHERE '
        'request_id = %s', (rid,))
    if (len(matching) != 1 or not admission or admission["payload_sha256"] != (
        manifest["plan"]["requests"][matching[0]]["payload_sha256"]
    )):
        raise CoreError("EVALUATION_PAYLOAD_CHANGED", 409)
    if json.loads(admission["authority_json"]) != json.loads(run["binding_json"])["authority"]:
        raise CoreError("OWNER_AUTHORITY_REVISION_CHANGED", 409)
    consent = ConsentService(store, settings, clock).require(
        run["consent_id"], CAPABILITY, DEVICE, run["manifest_sha256"])
    view = owner_authority.current(store, settings, clock, purpose="evaluation",
                                   authority=json.loads(run["binding_json"])["authority"])
    retention = min(settings.raw_event_retention_days, view.policy.source_retention_days)
    source_deadlines = [after(store.one('SELECT ingested_at FROM events WHERE event_id = %s',
                                       (event_id,))["ingested_at"],
                              86400 * retention)
                        for event_id in manifest["source_event_ids"]]
    deadlines = [run["lease_until"], consent["expires_at"], *source_deadlines]
    if view.policy.expires_at is not None:
        deadlines.append(view.policy.expires_at.isoformat())
    return {"lease_until": min(deadlines, key=lambda value: datetime.fromisoformat(value))}


def recover(store, now):
    # Core's original cancellation drain settles expired reservations through the
    # original budget journal. Never set usage states directly or repeat a call.
    job_queue.cancel_active(store, "kind='evaluation' AND state='RUNNING' AND lease_until<=%s",
                            (now,), "EVALUATION_INTERRUPTED", now)


async def run(core, job):
    from companion import budget

    store, settings, clock = core.store, core.settings, core.clock
    error = None
    try:
        if not isinstance(core.model, ModelClient):
            raise CoreError("EVALUATION_MODEL_CLIENT_REQUIRED", 503)
        with store.transaction():
            row, manifest = check_job(store, settings, clock, job["job_id"],
                                      lease_token=job["lease_token"])
            prior = store.rows('SELECT state FROM model_usage WHERE job_id = %s', (job["job_id"],))
            stored = store.one('SELECT COUNT(*) AS n FROM evaluation_results WHERE evaluation_id = '
                '%s', (row["evaluation_id"],))["n"]
            # Only a cleanly settled prefix with all results continues; never repeat a call.
            if any(usage["state"] != "SETTLED" for usage in prior) or stored != len(prior):
                raise CoreError("EVALUATION_PREVIOUS_ATTEMPT_REVIEW_REQUIRED", 409)
        requests = manifest["plan"]["requests"]
        stop = min(len(prior) + BATCH, len(requests))
        for ordinal in range(len(prior), stop):
            item = requests[ordinal]
            rid = request_id(row["manifest_sha256"], ordinal)
            def source_check(current_request=rid):
                check_job(store, settings, clock, job["job_id"], lease_token=job["lease_token"],
                          current_request=current_request)
            with store.transaction():
                source_check()
                job_queue.renew(store, job, after(clock(), settings.lease_seconds))
            messages = item["payload"]["messages"]
            budget.reserve(store, settings, job["job_id"], "evaluation", messages, clock(),
                           request_id=rid, logical_request_id=rid, clock=clock)
            response = await budget.complete(core.model, store, settings, rid, messages,
                                             "evaluation", clock, source_check=source_check)
            with store.transaction():
                source_check()
                usage = store.one('SELECT state FROM model_usage WHERE request_id = %s', (rid,))
                if usage["state"] != "SETTLED":
                    raise CoreError("EVALUATION_USAGE_UNCERTAIN", 409)
                safe_text = redact(response.text, settings)
                store.insert("evaluation_results", request_id=rid,
                             evaluation_id=row["evaluation_id"],
                             ordinal=ordinal, text=safe_text,
                             text_redacted=int(safe_text != response.text),
                             finish_reason=response.finish_reason,
                             evidence_type=("SYNTHETIC_MODEL_RESPONSE" if isinstance(
                                 core.model.client._transport_for_url(httpx.URL(settings.model_url)),
                                 httpx.MockTransport) else "MODEL_RESPONSE"), created_at=clock())
        with store.transaction():
            source_check()
            if stop < len(requests):
                # Yield the worker; the next claim resumes after any waiting chat.
                job_queue.requeue(store, job["job_id"], clock(), token=job["lease_token"])
            else:
                job_queue.release(
                    store, job["job_id"], "SUCCEEDED", clock(), token=job["lease_token"]
                )
    except CoreError as exc:
        error = exc.code
    except Exception:
        error = "EVALUATION_INTERNAL_ERROR"
    if error:
        with store.transaction():
            job_queue.release(store, job["job_id"], "FAILED", clock(), reason=error,
                              token=job["lease_token"])
            store.audit("evaluation.failed", job["job_id"], job["trace_id"], clock(), code=error)
    return True


def results(store, settings, clock, evaluation_id):
    _enabled(settings)
    captured_epoch = epoch(store)
    row = store.one('SELECT r.evaluation_id AS evaluation_id, r.manifest_sha256 AS '
        'manifest_sha256, r.consent_id AS consent_id, r.job_id AS job_id, r.manifest_json AS '
        'manifest_json, r.sources_json AS sources_json, r.binding_json AS binding_json, r.redacted '
        'AS redacted, r.created_at AS created_at, j.state AS state, j.last_error_code AS '
        'last_error_code FROM evaluation_runs AS r JOIN jobs AS j ON r.job_id = j.job_id WHERE '
        'r.evaluation_id = %s', (evaluation_id,))
    if not row:
        raise CoreError("EVALUATION_NOT_FOUND", 404)
    if row["redacted"]:
        raise CoreError("EVALUATION_ERASED", 410)
    manifest = json.loads(row["manifest_json"])
    sources, _, _ = _inputs(store, settings, manifest, clock())
    if sources != json.loads(row["sources_json"]):
        raise CoreError("EVALUATION_SOURCE_CHANGED", 409)
    outcomes = []
    for ordinal, item in enumerate(manifest["plan"]["requests"]):
        rid = request_id(row["manifest_sha256"], ordinal)
        usage = store.one('SELECT state, input_tokens, output_tokens, cached_input_tokens, '
            'billed_micro_usd FROM model_usage WHERE request_id = %s', (rid,))
        observed = store.one('SELECT text, text_redacted, finish_reason, evidence_type FROM '
            'evaluation_results WHERE request_id = %s',
                             (rid,))
        outcomes.append({"ordinal": ordinal, "request_id": rid,
                         **{k: item[k] for k in (
                             "case", "repetition", "strategy", "payload_sha256")},
                         "state": usage["state"] if usage else "NOT_STARTED", "usage": usage,
                         "evidence_type": (observed["evidence_type"] if observed
                                           else "FAILED_ATTEMPT"),
                         "text": observed["text"] if observed else "",
                         "text_redacted": bool(observed and observed["text_redacted"]),
                         "finish_reason": observed["finish_reason"] if observed else None,
                         "complete_response": bool(observed and observed["text"]
                                                   and not observed["text_redacted"] and usage
                                                   and usage["state"] == "SETTLED"
                                                   and observed["finish_reason"] == "stop")})
    # As in other private views, fence the completed projection rather than
    # keeping an initial SQLite snapshot alive across an independent erasure.
    current = store.one('SELECT redacted, manifest_sha256, sources_json FROM evaluation_runs WHERE '
        'evaluation_id = %s', (evaluation_id,))
    if not current or current["redacted"]:
        raise CoreError("EVALUATION_ERASED", 410)
    current_sources, _, _ = _inputs(store, settings, manifest, clock())
    if (current["manifest_sha256"] != row["manifest_sha256"]
        or current["sources_json"] != row["sources_json"] or current_sources != sources):
        raise CoreError("EVALUATION_SOURCE_CHANGED", 409)
    if captured_epoch != epoch(store):
        raise CoreError("PRIVACY_CHANGED", 409)
    return {"evaluation_id": evaluation_id, "manifest_sha256": row["manifest_sha256"],
            "plan_sha256": manifest["plan"]["plan_sha256"], "state": row["state"],
            "error_code": row["last_error_code"], "requests": outcomes}


def erase(store, event_ids):
    if not store.one('SELECT 1 FROM pg_catalog.pg_class WHERE '
        "relnamespace=current_schema()::regnamespace AND relname='evaluation_runs' AND relkind IN "
        "('r','p')"):
        return
    with nullcontext() if store.in_transaction else store.transaction():
        affected = store.rows('SELECT DISTINCT r.evaluation_id, r.job_id FROM evaluation_runs AS r '
            'JOIN job_sources AS s USING (job_id) WHERE s.event_id IN (SELECT value COLLATE "C" '
            'FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS '
            'parsed(value))', (encode(sorted(event_ids)),))
        for row in affected:
            store.execute("UPDATE evaluation_runs SET manifest_json = '{}', sources_json = '{}', "
                "binding_json = '{}', redacted = 1 WHERE evaluation_id = %s",
                          (row["evaluation_id"],))
            store.execute('DELETE FROM evaluation_results WHERE evaluation_id = %s',
                          (row["evaluation_id"],))
            job_queue.release(store, row["job_id"], "CANCELLED", reason="EVALUATION_SOURCE_ERASED")


def revoke_jobs(store, consent_id, now):
    if not store.one('SELECT 1 FROM pg_catalog.pg_class WHERE '
        "relnamespace=current_schema()::regnamespace AND relname='evaluation_runs' AND relkind IN "
        "('r','p')"):
        return  # Revocation remains available before this projection's migration.
    job_queue.cancel_active(store, (
        "kind='evaluation' AND job_id IN (SELECT job_id FROM evaluation_runs WHERE "
        'consent_id=%s)'
    ),
                            (consent_id,), "EVALUATION_CONSENT_REVOKED", now)


def stop_window(store, window_sha256, now):
    if not store.one('SELECT 1 FROM pg_catalog.pg_class WHERE '
        "relnamespace=current_schema()::regnamespace AND relname='evaluation_runs' AND relkind IN "
        "('r','p')"):
        return
    job_queue.cancel_active(store, (
        "kind='evaluation' AND job_id IN (SELECT job_id FROM evaluation_runs WHERE "
        '(CASE WHEN binding_json IS JSON THEN binding_json::json #>> '
        "'{window_sha256}' END)=%s)"
    ),
                            (window_sha256,), "LIVE_WINDOW_STOPPED", now)
