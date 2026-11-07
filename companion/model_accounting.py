"""Operator evidence and conservative repricing for a closed model attempt.

Billing evidence is independent of the missing model response. Neither an owner
estimate nor a provider statement makes UNKNOWN output successful or replayable.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from companion import budget, cloud_budget, model_carry, owner_authority, paid_budget
from companion.contracts import CoreError, StrictModel
from companion.store import utcnow
from companion_delivery_protocol import timestamp

KIND = "model_accounting_reconciliation"
PROJECTION = "model_accounting:"
PENDING = "model_accounting_pending:"
MAX_AMOUNT = 9_000_000_000_000_000
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Amount = Annotated[int, Field(ge=0, le=MAX_AMOUNT, strict=True)]
Name = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:/-]+$")]


class Estimate(StrictModel):
    kind: Literal["owner_estimate"]
    reference_provider: Name
    reference_model: Name
    currency: Literal["USD"]
    input_rate: Amount
    output_rate: Amount


class Statement(StrictModel):
    kind: Literal["provider_statement"]
    provider: Name
    model: Name
    currency: Literal["USD"]
    actual_micro_usd: Amount
    provider_record_sha256: Hash


class Proposal(StrictModel):
    version: Annotated[int, Field(strict=True, ge=1, le=1)]
    operation_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")]
    request_id: Annotated[str, Field(pattern=r"^model_[a-f0-9]{32}$")]
    authorization_sha256: Hash
    evidence_sha256: Hash
    basis: Annotated[Estimate | Statement, Field(discriminator="kind")]


class Prepared(Proposal):
    journal_id: Annotated[str, Field(pattern=r"^budget_[a-f0-9]{32}$")]
    expected_head_sha256: Hash
    binding_sha256: Hash
    native_settlement_sha256: Hash
    closure_sha256: Hash
    predecessor_sha256: Hash | None
    previous_accounted_micro_usd: Amount
    budget_limit_codes: Annotated[list[Literal[
        "ATTEMPT_AMOUNT", "WINDOW_AMOUNT", "REQUEST_AMOUNT", "DAILY_AMOUNT", "MONTHLY_AMOUNT",
        "SCENARIO_DAILY_AMOUNT", "SCENARIO_MONTHLY_AMOUNT", "EVALUATION_AMOUNT",
    ]], Field(max_length=8)]


class Pending(StrictModel):
    request: Prepared
    accounted_micro_usd: Amount


def evidence(path, expected):
    """Only its digest crosses into the journal; never print private statement bytes."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(10 * 1024 * 1024 + 1)
        if not raw or len(raw) > 10 * 1024 * 1024:
            raise ValueError("bounded evidence required")
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("evidence digest mismatch")
    except (OSError, ValueError) as error:
        raise CoreError("MODEL_ACCOUNTING_EVIDENCE_REQUIRED", 409) from error


def _original(store, request_id):
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
    if not admission or not usage or not admission["io_started_at"]:
        raise ValueError("original dispatched admission required")
    frozen = json.loads(admission["snapshot_json"])
    if paid_budget.sha(frozen) != admission["contract_sha256"] or frozen["currency"] != "USD":
        raise ValueError("original financial contract changed")
    stable_usage = {key: usage[key] for key in (
        "request_id", "usage_id", "scenario", "provider", "model", "occurred_at",
    )}
    return admission, usage, paid_budget.sha({"admission": admission, "usage": stable_usage})


def _priced(request, admission, usage):
    basis = request.basis
    if isinstance(basis, Estimate):
        # Frozen reservation counts remain estimates; no inferred cache discount.
        amount = (admission["input_bound"] * basis.input_rate
                  + admission["output_bound"] * basis.output_rate + 999_999) // 1_000_000
        status = "ESTIMATED"
    else:
        if (basis.provider, basis.model) != (usage["provider"], usage["model"]):
            raise ValueError("provider statement belongs to another route")
        amount, status = basis.actual_micro_usd, "PROVIDER_CONFIRMED"
    if amount > MAX_AMOUNT:
        raise ValueError("accounting amount overflow")
    return amount, status


def _prior(records, request_id):
    return next((row for row in reversed(list(records.values()))
                 if row["request"]["request_id"] == request_id), None)


def fold(store, row, record_sha, journal_id, latest, closures, blocked, records):
    """Validate at the original journal cutoff, without replacing native settlement."""
    fields = {"kind", "version", "request", "request_sha256", "basis_micro_usd",
              "accounted_micro_usd", "billing_status", "at", "previous"}
    if set(row) != fields or type(row["version"]) is not int or row["version"] != 1:
        raise ValueError("invalid model accounting record")
    request = Prepared.model_validate(row["request"])
    operation = request.operation_id
    if (operation in records or row["request_sha256"] != paid_budget.sha(row["request"])
        or request.journal_id != journal_id or request.expected_head_sha256 != row["previous"]):
        raise ValueError("model accounting identity or cutoff")
    admission, usage, binding = _original(store, request.request_id)
    financial = latest.get(request.request_id)
    previous = _prior(records, request.request_id)
    if (binding != request.binding_sha256 or not financial
        or financial["state"] != "UNKNOWN"
        or request.request_id in blocked and isinstance(request.basis, Estimate)
        or paid_budget.sha(financial) != request.native_settlement_sha256
        or closures.get(request.request_id) != request.closure_sha256
        or request.predecessor_sha256 != (previous["sha256"] if previous else None)
        or timestamp(row["at"]) < timestamp(admission["io_started_at"])
        or previous and timestamp(row["at"]) < timestamp(previous["at"])
        or request.previous_accounted_micro_usd < max(
            admission["reserved_micro_usd"], financial["minimum_micro_usd"],
            previous["accounted_micro_usd"] if previous else 0)):
        raise ValueError("model accounting original liability changed")
    amount, status = _priced(request, admission, usage)
    codes = request.budget_limit_codes
    inherited = set(previous["request"]["budget_limit_codes"]) if previous else set()
    frozen = json.loads(admission["snapshot_json"])
    if (codes != sorted(set(codes)) or inherited - set(codes)
        or isinstance(request.basis, Estimate) and codes
        or isinstance(request.basis, Statement) and (
            amount > frozen["per_attempt_micro_usd"] and "ATTEMPT_AMOUNT" not in codes
            or usage["scenario"] == "evaluation" and amount > 5000
            and "EVALUATION_AMOUNT" not in codes)):
        raise ValueError("model accounting budget stop changed")
    if (row["basis_micro_usd"] != amount or type(row["basis_micro_usd"]) is not int
        or row["billing_status"] != status
        or type(row["accounted_micro_usd"]) is not int
        or row["accounted_micro_usd"] != max(amount, request.previous_accounted_micro_usd)
        or previous and previous["billing_status"] == "PROVIDER_CONFIRMED"
        and status == "ESTIMATED"):
        raise ValueError("invalid model accounting valuation")
    if any(item["request"]["request_id"] == request.request_id
           and item["request"]["evidence_sha256"] == request.evidence_sha256
           for item in records.values()):
        raise ValueError("model accounting evidence reused")
    if isinstance(request.basis, Statement) and any(
        item["request"]["basis"]["kind"] == "provider_statement"
        and item["request"]["basis"]["provider"] == request.basis.provider
        and item["request"]["basis"]["provider_record_sha256"] == (
            request.basis.provider_record_sha256) for item in records.values()
    ):
        raise ValueError("provider billing record already attributed")
    records[operation] = {**row, "sha256": record_sha}
    if codes:
        blocked.setdefault(request.request_id, record_sha)


def validate_projection(store, records, replay=None):
    saved = {row["key"].removeprefix(PROJECTION): row["value"] for row in store.rows(
        'SELECT key, value FROM runtime_meta WHERE key >= %s AND key < %s',
        (PROJECTION, "model_accounting;"))}
    if saved.keys() - records.keys():
        raise ValueError("accounting journal behind database")
    for operation, record in records.items():
        usage = store.one('SELECT billed_micro_usd FROM model_usage WHERE request_id = %s',
                          (record["request"]["request_id"],))
        if record["request_sha256"] == replay:
            if saved.get(operation) not in {None, record["sha256"]} or not usage:
                raise ValueError("accounting replay conflict")
            continue
        if (saved.get(operation) != record["sha256"] or not usage
            or usage["billed_micro_usd"] < record["accounted_micro_usd"]
            or record["request"]["budget_limit_codes"] and not store.one(
                "SELECT 1 FROM runtime_meta WHERE key = 'model_budget_blocked'")):
            raise ValueError("accounting database behind journal")
    pending = {}
    for row in store.rows('SELECT key, value FROM runtime_meta WHERE key >= %s AND key < %s',
                          (PENDING, "model_accounting_pending;")):
        item = Pending.model_validate_json(row["value"])
        request = item.request
        admission, usage, binding = _original(store, request.request_id)
        amount, _ = _priced(request, admission, usage)
        if (row["key"] != PENDING + request.operation_id
            or not isinstance(request.basis, Statement) or binding != request.binding_sha256
            or item.accounted_micro_usd != max(amount, request.previous_accounted_micro_usd)
            or usage["billed_micro_usd"] < item.accounted_micro_usd
            or paid_budget.sha(request.model_dump(mode="json")) != replay):
            raise ValueError("known billing responsibility awaits identical journal recovery")
        pending[request.operation_id] = item.model_dump(mode="json")
    return pending


def _supported(store, settings):
    if (not owner_authority.required(store, settings) or not model_carry.active(settings)
        or store.one("SELECT 1 FROM runtime_meta WHERE key IN ('model_budget_restore_required', "
            "'cloud_budget_restore_required')")):
        raise CoreError("MODEL_ACCOUNTING_CUSTODY_REQUIRED", 409)
    model_carry.supported(store, settings)
    owner = store.one("SELECT value FROM runtime_meta WHERE key = 'owner_qq_id'")
    if not owner or owner["value"] != settings.owner_qq_id:
        raise CoreError("OWNER_IDENTITY_MISMATCH", 403)


def _prepare(store, settings, proposal, view):
    admission, usage, binding = _original(store, proposal.request_id)
    financial = view["model_latest"].get(proposal.request_id)
    closure = view["model_closures"].get(proposal.request_id)
    if (usage["state"] != "UNKNOWN" or not financial or financial["state"] != "UNKNOWN"
        or not closure or proposal.request_id in view["blocked_request_ids"]
        and isinstance(proposal.basis, Estimate)):
        raise CoreError("MODEL_ACCOUNTING_CLOSED_UNKNOWN_REQUIRED", 409)
    previous = _prior(view["model_accounting"], proposal.request_id)
    codes = []
    if isinstance(proposal.basis, Statement):
        codes = budget._actual_limit_violations(store, admission,
            json.loads(admission["snapshot_json"]), usage, proposal.basis.actual_micro_usd)
        if previous:
            codes += previous["request"]["budget_limit_codes"]
    request = Prepared(**proposal.model_dump(), journal_id=admission["journal_id"],
        expected_head_sha256=view["head_sha256"], binding_sha256=binding,
        native_settlement_sha256=paid_budget.sha(financial), closure_sha256=closure,
        predecessor_sha256=previous["sha256"] if previous else None,
        previous_accounted_micro_usd=usage["billed_micro_usd"],
        budget_limit_codes=sorted(set(codes)))
    amount, status = _priced(request, admission, usage)
    return request, amount, status


def preview(store, settings, value, evidence_path, clock=utcnow):
    try:
        proposal = Proposal.model_validate(value)
        evidence(evidence_path, proposal.evidence_sha256)
        with store.transaction():
            _supported(store, settings)
            view = paid_budget.journal(store, settings.paid_model, allow_settlement=True,
                                       initialize=False)
            request, amount, status = _prepare(store, settings, proposal, view)
            record = _record(request, amount, status, clock())
            _check_record(store, record, view)
            return {"state": "PREVIEW", "prepared_request": request.model_dump(mode="json"),
                    **_receipt(record)}
    except (ValidationError, ValueError, KeyError, TypeError) as error:
        raise CoreError("MODEL_ACCOUNTING_INVALID", 409) from error


def _record(request, amount, status, now):
    material = request.model_dump(mode="json")
    return {"kind": KIND, "version": 1, "request": material,
            "request_sha256": paid_budget.sha(material), "basis_micro_usd": amount,
            "accounted_micro_usd": max(amount, request.previous_accounted_micro_usd),
            "billing_status": status, "at": now}


def _receipt(record):
    return {"operation_id": record["request"]["operation_id"],
            "request_id": record["request"]["request_id"],
            "billing_status": record["billing_status"],
            "basis_micro_usd": record["basis_micro_usd"],
            "accounted_micro_usd": record["accounted_micro_usd"],
            "budget_limit_codes": record["request"]["budget_limit_codes"],
            "model_outcome": "UNKNOWN", "provider_calls": 0, "qq_sends": 0}


def _check_record(store, record, view):
    row = {**record, "previous": view["head_sha256"]}
    fold(store, row, paid_budget.sha(row), record["request"]["journal_id"],
         view["model_latest"], view["model_closures"],
         dict.fromkeys(view["blocked_request_ids"]), dict(view["model_accounting"]))


def _hold_known_statement(store, request, material, now):
    """Only reached after complete original/request validation, never for estimates."""
    held = material["accounted_micro_usd"]
    store.execute('UPDATE model_usage SET billed_micro_usd = GREATEST(billed_micro_usd, %s) WHERE '
        'request_id = %s', (held, request.request_id))
    pending = Pending(request=request, accounted_micro_usd=held)
    store.execute('INSERT INTO runtime_meta (key, value) VALUES (%s, %s) ON CONFLICT(key) DO '
        'UPDATE SET value = excluded.value, record_sequence = DEFAULT',
                  (PENDING + request.operation_id, pending.model_dump_json()))
    if request.budget_limit_codes:
        store.execute("INSERT INTO runtime_meta (key, value) VALUES ('model_budget_blocked', '1') "
            'ON CONFLICT(key) DO UPDATE SET value = excluded.value, record_sequence = DEFAULT')
    if not store.one("SELECT 1 FROM audit_log WHERE action = 'model.accounting_pending' AND "
        'entity_id = %s', (request.operation_id,)):
        store.audit("model.accounting_pending", request.operation_id, "operator", now,
                    request_sha256=material["request_sha256"], accounted_micro_usd=held)
    cloud_budget.sync_reference(store, "model", request.request_id)
    return {"state": "PENDING_JOURNAL", **_receipt(material)}


def record(store, settings, value, evidence_path, clock=utcnow):
    try:
        request = Prepared.model_validate(value)
        evidence(evidence_path, request.evidence_sha256)
        request_sha = paid_budget.sha(request.model_dump(mode="json"))
        with store.transaction():
            _supported(store, settings)
            view = paid_budget.journal(store, settings.paid_model, allow_settlement=True,
                                       initialize=False, accounting_replay=request_sha)
            existing = view["model_accounting"].get(request.operation_id)
            if existing:
                if existing["request_sha256"] != request_sha:
                    raise CoreError("MODEL_ACCOUNTING_OPERATION_CONFLICT", 409)
                # Completed repeats already have a durable projection; interruptions need fsync.
                projected = store.one('SELECT value FROM runtime_meta WHERE key = %s',
                                      (PROJECTION + request.operation_id,))
                if not projected or request.operation_id in view["model_accounting_pending"]:
                    try:
                        with Path(settings.paid_model.journal_path).open("r+b") as stream:
                            os.fsync(stream.fileno())
                    except OSError:
                        if isinstance(request.basis, Statement):
                            return _hold_known_statement(store, request, existing, clock())
                        raise
                saved = existing
            else:
                proposal = Proposal.model_validate(
                    request.model_dump(include=set(Proposal.model_fields)))
                current, amount, status = _prepare(store, settings, proposal, view)
                if request.operation_id in view["model_accounting_pending"]:
                    pending = view["model_accounting_pending"][request.operation_id]
                    if pending["request"] != request.model_dump(mode="json"):
                        raise CoreError("MODEL_ACCOUNTING_OPERATION_CONFLICT", 409)
                    current = current.model_copy(update={
                        "previous_accounted_micro_usd": request.previous_accounted_micro_usd})
                if current != request:
                    raise CoreError("MODEL_ACCOUNTING_PREVIEW_CHANGED", 409)
                material = _record(request, amount, status, clock())
                _check_record(store, material, view)
                try:
                    view = paid_budget.journal(store, settings.paid_model, material,
                        allow_settlement=True, initialize=False, accounting_replay=request_sha)
                except CoreError:
                    if isinstance(request.basis, Statement):
                        return _hold_known_statement(store, request, material, clock())
                    raise
                saved = view["model_accounting"][request.operation_id]
            store.execute('UPDATE model_usage SET billed_micro_usd = GREATEST(billed_micro_usd, '
                '%s) WHERE request_id = %s', (saved["accounted_micro_usd"], request.request_id))
            store.execute('INSERT INTO runtime_meta VALUES (%s, %s) ON CONFLICT DO NOTHING',
                          (PROJECTION + request.operation_id, saved["sha256"]))
            if request.budget_limit_codes:
                store.execute('INSERT INTO runtime_meta (key, value) VALUES '
                    "('model_budget_blocked', '1') ON CONFLICT(key) DO UPDATE SET value = "
                    'excluded.value, record_sequence = DEFAULT')
            store.execute('DELETE FROM runtime_meta WHERE key = %s',
                          (PENDING + request.operation_id,))
            if not store.one("SELECT 1 FROM audit_log WHERE action = 'model.accounting_recorded' "
                'AND entity_id = %s', (request.operation_id,)):
                store.audit("model.accounting_recorded", request.operation_id, "operator", clock(),
                            request_id=request.request_id, receipt_sha256=saved["sha256"],
                            billing_status=saved["billing_status"],
                            accounted_micro_usd=saved["accounted_micro_usd"])
            paid_budget.journal(store, settings.paid_model, allow_settlement=True, initialize=False)
            cloud_budget.sync_reference(store, "model", request.request_id)
            return {"state": "RECORDED", "receipt_sha256": saved["sha256"], **_receipt(saved)}
    except (ValidationError, ValueError, KeyError, TypeError) as error:
        raise CoreError("MODEL_ACCOUNTING_INVALID", 409) from error


def load_request(path, expected):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(16_385)
        if len(raw) > 16_384 or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("request digest mismatch")
        return json.loads(raw)
    except (OSError, ValueError) as error:
        raise CoreError("MODEL_ACCOUNTING_INVALID", 409) from error
