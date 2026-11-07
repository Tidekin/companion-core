"""Offline correction of the historical video/chat rate comparison defect.

The correction retains the larger monetary responsibility. It never supplies a
video result, clears a window STOP, retries a job, or grants network authority.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from companion import budget, cumulative, live_text, paid_budget
from companion.contracts import CoreError
from companion.store import utcnow

KIND = "video_rate_correction"
SHA = r"[a-f0-9]{64}"
FIELDS = {
    "kind", "version", "request_id", "state", "minimum_micro_usd", "contract_sha256",
    "io_started_at", "journal_id", "blocked_sha256", "admission_sha256", "usage_sha256",
    "diagnostic_sha256", "authorization_sha256", "deployment_sha256", "idempotency_key",
    "expected_head_sha256", "at", "previous",
}


def evidence(store, request_id, prior):
    """Reconstruct the frozen evidence without interpreting estimates as observations."""
    admission = store.one('SELECT model_admissions.request_id AS request_id, '
        'model_admissions.logical_request_id AS logical_request_id, model_admissions.attempt_id AS '
        'attempt_id, model_admissions.attempt_number AS attempt_number, model_admissions.window_id '
        'AS window_id, model_admissions.contract_sha256 AS contract_sha256, '
        'model_admissions.payload_sha256 AS payload_sha256, model_admissions.snapshot_json AS '
        'snapshot_json, model_admissions.reserved_micro_usd AS reserved_micro_usd, '
        'model_admissions.input_bound AS input_bound, model_admissions.output_bound AS '
        'output_bound, model_admissions.lease_token AS lease_token, model_admissions.lease_until '
        'AS lease_until, model_admissions.io_started_at AS io_started_at, '
        'model_admissions.journal_path AS journal_path, model_admissions.journal_id AS journal_id, '
        'model_admissions.measurement_json AS measurement_json, model_admissions.authority_json AS '
        'authority_json FROM model_admissions AS model_admissions WHERE '
        'model_admissions.request_id = %s', (request_id,))
    usage = store.one('SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS '
        'request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, '
        'model_usage.provider AS provider, model_usage.model AS model, model_usage.input_tokens AS '
        'input_tokens, model_usage.cached_input_tokens AS cached_input_tokens, '
        'model_usage.output_tokens AS output_tokens, model_usage.billed_micro_usd AS '
        'billed_micro_usd, model_usage.is_estimate AS is_estimate, model_usage.occurred_at AS '
        'occurred_at, model_usage.state AS state, model_usage.pricing_known AS pricing_known FROM '
        'model_usage AS model_usage WHERE model_usage.request_id = %s', (request_id,))
    diagnostics = store.rows('SELECT safe_detail_json FROM audit_log WHERE action = '
        "'model.usage_diagnostic' AND entity_id = %s ORDER BY audit_id NULLS FIRST", (request_id,))
    if not admission or not usage or len(diagnostics) != 1:
        raise ValueError("missing original video evidence")
    snapshot = json.loads(admission["snapshot_json"])
    diagnostic = json.loads(diagnostics[0]["safe_detail_json"])
    video = snapshot.get("video_input")
    if (not video or snapshot.get("cumulative")
        or paid_budget.sha(snapshot) != admission["contract_sha256"]
        or usage["scenario"] != "content_video" or not admission["io_started_at"]
        or (usage["provider"], usage["model"]) != (video["provider"], video["model"])
        or not usage["job_id"] or admission["logical_request_id"] != usage["job_id"]
        or (snapshot["input_rate"], snapshot["cached_input_rate"], snapshot["output_rate"])
           == (video["input_rate"], video["cached_input_rate"], video["output_rate"])
        or prior["kind"] != "blocked" or prior["state"] != "UNKNOWN"
        or prior.get("evidence_sha256") or prior.get("quota")
        or prior["contract_sha256"] != admission["contract_sha256"]
        or prior["io_started_at"] != admission["io_started_at"]):
        raise ValueError("not the historical video rate defect")
    counts = [usage[k] for k in ("input_tokens", "output_tokens", "cached_input_tokens")]
    if (any(type(n) is not int or n < 0 for n in counts) or not all(counts[:2])
        or counts[2] > counts[0] or counts[1] > admission["output_bound"]):
        raise ValueError("invalid observed usage")
    priced = budget.cost(SimpleNamespace(pricing_known=True, **video), *counts)
    expected = {
        "code": "NORMAL", "actual_usage_known": True,
        "input_estimated_tokens": admission["input_bound"], "input_actual_tokens": counts[0],
        "input_error_tokens": counts[0] - admission["input_bound"],
        "output_actual_tokens": counts[1], "cached_input_tokens": counts[2],
        "reserved_micro_usd": admission["reserved_micro_usd"], "usage_priced_micro_usd": priced,
        "cost_error_micro_usd": priced - admission["reserved_micro_usd"],
        "currency": "USD", "billing_basis": "configured_rates_not_invoice", "limits": [],
    }
    if diagnostic != expected or snapshot["currency"] != "USD":
        raise ValueError("conflicting observed usage evidence")
    original = {**usage, "state": "UNKNOWN", "billed_micro_usd": prior["minimum_micro_usd"]}
    return admission, usage, snapshot, {
        "admission_sha256": paid_budget.sha(admission),
        "usage_sha256": paid_budget.sha(original), "diagnostic_sha256": paid_budget.sha(diagnostic),
    }, max(prior["minimum_micro_usd"], admission["reserved_micro_usd"], priced)


def validate_record(store, row, prior, blocked_sha256, journal_id):
    """Called during every independent-journal replay, before removing one exact block."""
    if (set(row) != FIELDS or type(row["version"]) is not int or row["version"] != 1
        or row["kind"] != KIND or not prior
        or row["state"] != "SETTLED" or row["journal_id"] != journal_id
        or row["expected_head_sha256"] != row["previous"]
        or row["blocked_sha256"] != blocked_sha256
        or any(not isinstance(row[k], str) or not re.fullmatch(SHA, row[k]) for k in (
            "blocked_sha256", "admission_sha256", "usage_sha256", "diagnostic_sha256",
            "authorization_sha256", "deployment_sha256", "idempotency_key",
        )) or not datetime.fromisoformat(row["at"]).tzinfo):
        raise ValueError("invalid video correction receipt")
    admission, usage, _, hashes, amount = evidence(store, row["request_id"], prior)
    if (any(row[k] != v for k, v in hashes.items())
        or type(row["minimum_micro_usd"]) is not int or row["minimum_micro_usd"] != amount
        or usage["billed_micro_usd"] < amount
        or row["contract_sha256"] != admission["contract_sha256"]
        or row["io_started_at"] != admission["io_started_at"]
        or row["journal_id"] != admission["journal_id"]):
        raise ValueError("video correction evidence changed")


def reconcile(store, settings, request, clock=utcnow):
    """Local operator only; no HTTP endpoint and no caller-supplied prices or amounts."""
    fields = {"request_id", "journal_id", "expected_head_sha256", "authorization_sha256",
              "deployment_sha256", "idempotency_key"}
    if (not isinstance(request, dict) or set(request) != fields
        or any(not isinstance(value, str) for value in request.values())
        or not re.fullmatch(r"model_[a-f0-9]{32}", request["request_id"])
        or not re.fullmatch(r"budget_[a-f0-9]{32}", request["journal_id"])
        or any(not re.fullmatch(SHA, request[k]) for k in fields - {"request_id", "journal_id"})):
        raise CoreError("VIDEO_RATE_CORRECTION_INVALID", 400)
    try:
        with store.transaction():
            approval = settings.paid_model
            if (not approval or not live_text.stopped(store) or store.one(
                "SELECT 1 FROM runtime_meta WHERE key = 'model_budget_restore_required'"
            ) or store.one('SELECT 1 FROM runtime_meta WHERE key = %s', (cumulative.PROJECTION,))):
                raise ValueError("stopped original deployment required")
            rows = [json.loads(line) for line in Path(
                approval.journal_path).read_text("utf-8").splitlines()]
            previous = [r for r in rows if r.get("kind") == KIND and (
                r.get("request_id") == request["request_id"]
                or r.get("idempotency_key") == request["idempotency_key"])]
            custody = SimpleNamespace(journal_path=approval.journal_path)
            if previous:
                if len(previous) != 1 or any(previous[0][k] != v for k, v in request.items()):
                    raise ValueError("conflicting correction replay")
                record = {k: v for k, v in previous[0].items() if k not in {"sha256", "previous"}}
                # journal validates the full chain and the original bound evidence,
                # even after an fsync-before-DB-commit crash.
                view = paid_budget.journal(store, custody, record, allow_settlement=True)
            else:
                view = paid_budget.journal(store, custody, allow_settlement=True)
                if (view["head_sha256"] != request["expected_head_sha256"]
                    or rows[0]["journal_id"] != request["journal_id"]
                    or not view["lifecycle"] or view["lifecycle"]["state"] != "STOPPED"
                    or store.one("SELECT 1 FROM model_usage WHERE state <> 'SETTLED' AND "
                        'request_id <> %s', (request["request_id"],))
                    or any(store.one(f"SELECT 1 FROM {table} WHERE state NOT IN ({states})")
                        for table, states in (("jobs", "'SUCCEEDED','FAILED','CANCELLED'"),
                                              ("outbox", "'SENT','FAILED','CANCELLED'"),
                                              ("tool_tasks", "'SUCCEEDED','FAILED','CANCELLED'")))
                    or store.one("SELECT 1 FROM content_items WHERE state = 'RUNNING'")):
                    raise ValueError("unresolved or active responsibility")
                blocked = [r for r in rows if r.get("kind") == "blocked"]
                corrections = {r["blocked_sha256"] for r in rows if r.get("kind") == KIND}
                unresolved = [r for r in blocked if r["sha256"] not in corrections]
                if len(unresolved) != 1 or unresolved[0]["request_id"] != request["request_id"]:
                    raise ValueError("unrelated independent block")
                prior = unresolved[0]
                admission, usage, frozen, hashes, amount = evidence(
                    store, request["request_id"], prior,
                )
                job = store.one('SELECT state FROM jobs WHERE job_id = %s', (usage["job_id"],))
                if (usage["state"] != "UNKNOWN" or not job or job["state"] != "CANCELLED"
                    or usage["billed_micro_usd"] != prior["minimum_micro_usd"]
                    or frozen != paid_budget.snapshot(settings)
                    or admission["journal_id"] != request["journal_id"]
                    or budget._actual_limit_violations(store, admission, frozen, usage, amount)
                    or store.one('SELECT 1 FROM outbox WHERE origin_event_id IN (SELECT event_id '
                        'FROM jobs WHERE job_id = %s)', (usage["job_id"],))):
                    raise ValueError("original frozen deployment or fee evidence changed")
                record = {"kind": KIND, "version": 1, **request, **hashes,
                    "state": "SETTLED", "minimum_micro_usd": amount,
                    "contract_sha256": admission["contract_sha256"],
                    "io_started_at": admission["io_started_at"], "blocked_sha256": prior["sha256"],
                    "at": clock()}
                view = paid_budget.journal(store, custody, record, allow_settlement=True)
            charged = store.one('SELECT billed_micro_usd FROM model_usage WHERE request_id = %s',
                                (request["request_id"],))
            if (not charged or charged["billed_micro_usd"] != record["minimum_micro_usd"]
                or view["blocked_request_ids"] or store.one(
                "SELECT 1 FROM model_usage WHERE state <> 'SETTLED' AND request_id <> %s",
                (request["request_id"],),
            )):
                raise ValueError("other unresolved responsibility")
            store.execute("UPDATE model_usage SET state = 'SETTLED', billed_micro_usd = %s WHERE "
                'request_id = %s',
                          (record["minimum_micro_usd"], request["request_id"]))
            store.execute("DELETE FROM runtime_meta WHERE key = 'model_budget_blocked'")
            paid_budget.journal(store, custody, allow_settlement=True)
            if not store.one("SELECT 1 FROM audit_log WHERE action = 'model.video_rate_correction' "
                'AND entity_id = %s', (request["request_id"],)):
                store.audit("model.video_rate_correction", request["request_id"], "operator",
                            clock(), receipt_sha256=paid_budget.sha(record),
                            retained_micro_usd=record["minimum_micro_usd"])
            return {"state": "CORRECTED_STOPPED", "request_id": request["request_id"],
                "retained_micro_usd": record["minimum_micro_usd"],
                "receipt_sha256": paid_budget.sha(record),
                "journal_head_sha256": view["head_sha256"],
                "model_attempts": 1, "provider_calls": 0, "qq_sends": 0}
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise CoreError("VIDEO_RATE_CORRECTION_REJECTED", 409) from error
