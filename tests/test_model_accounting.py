"""Billing evidence appends once; execution uncertainty and its original custody survive."""

import copy
import hashlib
import json
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

from companion import budget, model_accounting, model_carry, owner_authority, paid_budget
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from companion.store import encode
from tests.model_carry_support import MESSAGES, migrated, stranded
from tests.owner_model_fixtures import authorized_job
from tests.support.owner_authority import activate
from tests.support.paid_model import approved_live
from tests.test_model_carry_lifecycle import current_unknown
from tests.test_model_renewal import renewal


def proposal(tmp_path, request_id, *, operation="estimate-first", basis=None):
    private = tmp_path / (operation + ".evidence")
    private.write_bytes(("private-synthetic-billing-evidence:" + operation).encode())
    return {
        "version": 1, "operation_id": operation, "request_id": request_id,
        "authorization_sha256": "a" * 64,
        "evidence_sha256": hashlib.sha256(private.read_bytes()).hexdigest(),
        "basis": basis or {"kind": "owner_estimate", "reference_provider": "reference",
            "reference_model": "reference-text", "currency": "USD",
            "input_rate": 2_000_000, "output_rate": 4_000_000},
    }, private


def prepared(core, tmp_path, request_id, **options):
    value, private = proposal(tmp_path, request_id, **options)
    return model_accounting.preview(core.store, core.settings, value, private, core.clock)[
        "prepared_request"], private


def journal(core):
    return Path(core.settings.paid_model.journal_path)


def snapshot(core):
    return list(core.store.db.iterdump()), journal(core).read_bytes()


def custody(core):
    with core.store.transaction():
        return paid_budget.journal(core.store, core.settings.paid_model, allow_settlement=True)


def test_estimate_keeps_unknown_counts_and_original_settlement_and_is_idempotent(core, tmp_path):
    case = migrated(core, tmp_path)
    before = snapshot(core)
    request, private = prepared(core, tmp_path, case.request_id)
    assert snapshot(core) == before
    original = core.store.one("SELECT * FROM model_usage WHERE request_id=?", (case.request_id,))
    native = custody(core)["model_latest"][case.request_id]
    first = model_accounting.record(core.store, core.settings, request, private, core.clock)
    assert first["billing_status"] == "ESTIMATED" and first["model_outcome"] == "UNKNOWN"
    assert first["accounted_micro_usd"] > original["billed_micro_usd"]
    after = core.store.one("SELECT * FROM model_usage WHERE request_id=?", (case.request_id,))
    assert after == original | {"billed_micro_usd": first["accounted_micro_usd"]}
    assert custody(core)["model_latest"][case.request_id] == native
    recorded = snapshot(core)
    assert model_accounting.record(core.store, core.settings, request, private, core.clock) == first
    assert snapshot(core) == recorded
    assert private.read_bytes() not in journal(core).read_bytes()
    assert core.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == 1
    cost = core.store.one("SELECT state,accounted_micro_usd FROM cloud_cost_snapshots "
                          "WHERE reference_type='model' AND reference_id=?", (case.request_id,))
    assert cost == {"state": "UNKNOWN", "accounted_micro_usd": first["accounted_micro_usd"]}
    with pytest.raises(CoreError):
        budget.reserve(core.store, core.settings, case.job_id, "chat", MESSAGES, core.clock())


def test_provider_statement_follows_estimate_without_refunding_or_inventing_response(
    core, tmp_path,
):
    case = migrated(core, tmp_path)
    request, private = prepared(core, tmp_path, case.request_id)
    first = model_accounting.record(core.store, core.settings, request, private, core.clock)
    bill = {"kind": "provider_statement", "provider": "offline-fixture", "model": "offline-text",
            "currency": "USD", "actual_micro_usd": 0, "provider_record_sha256": "b" * 64}
    request, private = prepared(core, tmp_path, case.request_id,
                                operation="provider-final", basis=bill)
    confirmed = model_accounting.record(core.store, core.settings, request, private, core.clock)
    assert confirmed["billing_status"] == "PROVIDER_CONFIRMED"
    assert confirmed["basis_micro_usd"] == 0
    assert confirmed["accounted_micro_usd"] == first["accounted_micro_usd"]
    assert core.store.one("SELECT state,is_estimate FROM model_usage")["state"] == "UNKNOWN"
    assert core.store.one("SELECT is_estimate FROM model_usage")["is_estimate"] == 1
    before = snapshot(core)
    with pytest.raises(CoreError):
        prepared(core, tmp_path, case.request_id, operation="estimate-after-final")
    assert snapshot(core) == before


def test_provider_record_and_operation_cannot_be_reused_for_different_material(core, tmp_path):
    case = migrated(core, tmp_path)
    bill = {"kind": "provider_statement", "provider": "offline-fixture", "model": "offline-text",
            "currency": "USD", "actual_micro_usd": 5000, "provider_record_sha256": "b" * 64}
    request, private = prepared(core, tmp_path, case.request_id, basis=bill)
    result = model_accounting.record(core.store, core.settings, request, private, core.clock)
    assert result["basis_micro_usd"] == result["accounted_micro_usd"] == 5000
    changed = copy.deepcopy(request)
    changed["basis"]["actual_micro_usd"] += 1
    before = snapshot(core)
    with pytest.raises(CoreError, match="MODEL_ACCOUNTING_OPERATION_CONFLICT"):
        model_accounting.record(core.store, core.settings, changed, private, core.clock)
    assert snapshot(core) == before
    other = current_unknown(core)
    before = snapshot(core)
    with pytest.raises(CoreError):
        prepared(core, tmp_path, other, operation="same-provider-record", basis=bill)
    assert snapshot(core) == before


def test_confirmed_limit_violation_blocks_future_io_and_allows_larger_bill(core, tmp_path):
    case = migrated(core, tmp_path)
    job = authorized_job(core, number=2)
    actual = core.settings.paid_model.per_attempt_micro_usd + 1
    bill = {"kind": "provider_statement", "provider": "offline-fixture", "model": "offline-text",
            "currency": "USD", "actual_micro_usd": actual, "provider_record_sha256": "b" * 64}
    request, private = prepared(core, tmp_path, case.request_id, basis=bill)
    assert request["budget_limit_codes"] == ["ATTEMPT_AMOUNT"]
    result = model_accounting.record(core.store, core.settings, request, private, core.clock)
    assert result["accounted_micro_usd"] == actual
    assert case.request_id in custody(core)["blocked_request_ids"]
    assert core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")
    with pytest.raises(CoreError, match="RECONCILIATION_REQUIRED"):
        budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())
    before = snapshot(core)
    assert model_accounting.record(core.store, core.settings, request, private, core.clock) == (
        result)
    assert snapshot(core) == before
    bill.update(actual_micro_usd=actual + 100, provider_record_sha256="c" * 64)
    request, private = prepared(core, tmp_path, case.request_id,
                                operation="higher-confirmed-bill", basis=bill)
    higher = model_accounting.record(core.store, core.settings, request, private, core.clock)
    assert higher["accounted_micro_usd"] == actual + 100
    assert higher["budget_limit_codes"] == ["ATTEMPT_AMOUNT"]
    # A rolled-back DB stop cannot release the independently recorded violation.
    core.store.execute("DELETE FROM runtime_meta WHERE key='model_budget_blocked'")
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        custody(core)


@pytest.mark.parametrize("failure", ["before", "after_fsync", "partial"])
def test_confirmed_bill_survives_journal_failure_and_exact_recovery(
    core, tmp_path, monkeypatch, failure,
):
    case = migrated(core, tmp_path)
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="stop-before-known-bill", authority=case.successor["authority"]))
    bill = {"kind": "provider_statement", "provider": "offline-fixture", "model": "offline-text",
            "currency": "USD", "actual_micro_usd": 5000, "provider_record_sha256": "b" * 64}
    request, private = prepared(core, tmp_path, case.request_id, basis=bill)
    original = paid_budget.journal
    prefix = journal(core).read_bytes()

    def unavailable(store, approval, record=None, **kwargs):
        if record and record.get("kind") == model_accounting.KIND:
            if failure == "after_fsync":
                original(store, approval, record, **kwargs)
            elif failure == "partial":
                with journal(core).open("ab") as stream:
                    stream.write(b'{"kind":')
            raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)
        return original(store, approval, record, **kwargs)

    monkeypatch.setattr(paid_budget, "journal", unavailable)
    pending = model_accounting.record(core.store, core.settings, request, private, core.clock)
    monkeypatch.setattr(paid_budget, "journal", original)
    assert pending["state"] == "PENDING_JOURNAL"
    assert core.store.one("SELECT billed_micro_usd,state FROM model_usage") == {
        "billed_micro_usd": 5000, "state": "UNKNOWN"}
    assert core.store.one("SELECT accounted_micro_usd FROM cloud_cost_snapshots")[
        "accounted_micro_usd"] == 5000
    assert core.store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                          (model_accounting.PENDING + request["operation_id"],))
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        custody(core)
    changed = copy.deepcopy(request)
    changed["authorization_sha256"] = "c" * 64
    with pytest.raises(CoreError):
        model_accounting.record(core.store, core.settings, changed, private, core.clock)
    if failure == "partial":
        damaged = journal(core).read_bytes()
        with pytest.raises(CoreError):
            model_accounting.record(core.store, core.settings, request, private, core.clock)
        assert journal(core).read_bytes() == damaged
        # A trusted repair restores the verified complete prefix; the CLI never truncates it.
        journal(core).write_bytes(prefix)
    result = model_accounting.record(core.store, core.settings, request, private, core.clock)
    assert result["state"] == "RECORDED" and result["accounted_micro_usd"] == 5000
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                              (model_accounting.PENDING + request["operation_id"],))
    assert owner_authority._latest(core.store)["state"] == "STOPPED"
    assert len(custody(core)["model_accounting"]) == 1
    before = snapshot(core)
    assert model_accounting.record(core.store, core.settings, request, private, core.clock) == (
        result)
    assert snapshot(core) == before


def test_estimate_append_failure_does_not_create_a_known_bill_hold(core, tmp_path, monkeypatch):
    case = migrated(core, tmp_path)
    request, private = prepared(core, tmp_path, case.request_id)
    original = paid_budget.journal

    def unavailable(store, approval, record=None, **kwargs):
        if record and record.get("kind") == model_accounting.KIND:
            raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)
        return original(store, approval, record, **kwargs)

    before = snapshot(core)
    monkeypatch.setattr(paid_budget, "journal", unavailable)
    with pytest.raises(CoreError):
        model_accounting.record(core.store, core.settings, request, private, core.clock)
    assert snapshot(core) == before


def test_duplicate_evidence_does_not_create_another_operation(core, tmp_path):
    case = migrated(core, tmp_path)
    value, private = proposal(tmp_path, case.request_id)
    request = model_accounting.preview(core.store, core.settings, value, private, core.clock)[
        "prepared_request"]
    model_accounting.record(core.store, core.settings, request, private, core.clock)
    before = snapshot(core)
    value["operation_id"] = "duplicate-evidence"
    with pytest.raises(CoreError):
        model_accounting.preview(core.store, core.settings, value, private, core.clock)
    assert snapshot(core) == before


@pytest.mark.parametrize("change", ["evidence", "head", "binding", "amount", "provider",
                                    "closure", "version", "extra", "restore", "cumulative",
                                    "currency", "boolean_price"])
def test_invalid_accounting_material_cannot_mutate_state(core, tmp_path, change):
    case = migrated(core, tmp_path)
    request, private = prepared(core, tmp_path, case.request_id)
    if change == "evidence":
        private.write_bytes(b"changed evidence")
    elif change == "head":
        current_unknown(core)
    elif change == "binding":
        request["binding_sha256"] = "0" * 64
    elif change == "amount":
        request["previous_accounted_micro_usd"] = 0
    elif change == "provider":
        request["basis"] = {"kind": "provider_statement", "provider": "wrong-route",
            "model": "offline-text", "currency": "USD", "actual_micro_usd": 1,
            "provider_record_sha256": "b" * 64}
    elif change == "closure":
        request["closure_sha256"] = "0" * 64
    elif change == "version":
        request["version"] = True
    elif change == "extra":
        request["credential"] = "must-not-be-accepted"
    elif change == "restore":
        core.store.execute("INSERT INTO runtime_meta VALUES ('model_budget_restore_required','1')")
    elif change == "cumulative":
        core.store.execute("INSERT INTO runtime_meta VALUES ('model_budget_cumulative','{}')")
    elif change == "currency":
        request["basis"]["currency"] = "CNY"
    else:
        request["basis"]["input_rate"] = True
    before = snapshot(core)
    with pytest.raises(CoreError):
        model_accounting.record(core.store, core.settings, request, private, core.clock)
    assert snapshot(core) == before


def test_no_operator_entry_can_close_an_unfinished_transport(core, tmp_path):
    migrated(core, tmp_path)
    job = authorized_job(core, number=2)
    request = budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())
    budget.settle(core.store, core.settings, request)
    before = snapshot(core)
    with pytest.raises(CoreError):
        prepared(core, tmp_path, request)
    assert snapshot(core) == before


def test_accounting_requires_first_carry_and_preserves_its_prepared_receipt(core, tmp_path):
    case = stranded(core, tmp_path)
    assert not custody(core)["model_closures"]
    before = snapshot(core)
    with pytest.raises(CoreError, match="MODEL_ACCOUNTING_CLOSED_UNKNOWN_REQUIRED"):
        prepared(core, tmp_path, case.request_id)
    assert snapshot(core) == before
    carried = model_carry.prepare(core.store, core.settings, case.request,
                                  case.verified, core.clock())
    request, private = prepared(core, tmp_path, case.request_id)
    result = model_accounting.record(core.store, core.settings, request, private, core.clock)
    assert result["accounted_micro_usd"] > carried["retained_micro_usd"]
    assert model_carry.prepare(core.store, core.settings, case.request,
                               case.verified, core.clock()) == carried
    activate(core, case.policy, revision=2, operation="activate-repriced-first-carry")
    assert core.store.one("SELECT billed_micro_usd,state FROM model_usage") == {
        "billed_micro_usd": result["accounted_micro_usd"], "state": "UNKNOWN"}


def test_fresh_local_active_approval_needs_no_historical_carry(core, tmp_path):
    approved_live(core, tmp_path, concurrency=1, concurrency_basis="local_active_attempts_v2")
    assert authorized_job(core)
    assert not core.store.one("SELECT 1 FROM model_admissions")


def test_existing_carry_cannot_be_dropped_from_a_new_activation(core, tmp_path):
    case = migrated(core, tmp_path)
    before = core.store.rows("SELECT * FROM model_usage")
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="stop-before-dropping-carry", authority=case.successor["authority"]))
    with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_REQUIRED"):
        activate(core, case.policy.model_copy(update={"unknown_carry_sha256": None}),
                 revision=4, operation="drop-historical-carry")
    assert core.store.rows("SELECT * FROM model_usage") == before


def test_only_identical_operation_repairs_fsync_before_projection_crash(
    core, tmp_path, monkeypatch,
):
    case = migrated(core, tmp_path)
    request, private = prepared(core, tmp_path, case.request_id)
    native = core.store.execute

    def crash(sql, params=()):
        if sql.startswith("UPDATE model_usage SET billed_micro_usd=MAX"):
            raise OSError("synthetic DB write failure after journal fsync")
        return native(sql, params)

    monkeypatch.setattr(core.store, "execute", crash)
    with pytest.raises(OSError):
        model_accounting.record(core.store, core.settings, request, private, core.clock)
    monkeypatch.setattr(core.store, "execute", native)
    durable = journal(core).read_bytes()
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        custody(core)
    changed = copy.deepcopy(request)
    changed["authorization_sha256"] = "c" * 64
    with pytest.raises(CoreError):
        model_accounting.record(core.store, core.settings, changed, private, core.clock)
    result = model_accounting.record(core.store, core.settings, request, private, core.clock)
    assert result["state"] == "RECORDED" and journal(core).read_bytes() == durable
    assert len(custody(core)["model_accounting"]) == 1


def test_old_journal_prefix_and_dropped_projection_fail_closed(core, tmp_path):
    case = migrated(core, tmp_path)
    old = journal(core).read_bytes()
    request, private = prepared(core, tmp_path, case.request_id)
    model_accounting.record(core.store, core.settings, request, private, core.clock)
    current = journal(core).read_bytes()
    journal(core).write_bytes(old)
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        custody(core)
    journal(core).write_bytes(current)
    core.store.execute("DELETE FROM runtime_meta WHERE key=?",
                       (model_accounting.PROJECTION + request["operation_id"],))
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        custody(core)


def test_rehashed_receipt_cannot_change_native_amount_formula(core, tmp_path):
    case = migrated(core, tmp_path)
    request, private = prepared(core, tmp_path, case.request_id)
    model_accounting.record(core.store, core.settings, request, private, core.clock)
    rows = [json.loads(line) for line in journal(core).read_text(encoding="utf-8").splitlines()]
    rows[-1]["accounted_micro_usd"] -= 1
    rows[-1]["sha256"] = paid_budget.sha({k: v for k, v in rows[-1].items() if k != "sha256"})
    journal(core).write_text("".join(encode(row) + "\n" for row in rows), encoding="utf-8")
    core.store.execute("UPDATE runtime_meta SET value=? WHERE key=?", (rows[-1]["sha256"],
                       model_accounting.PROJECTION + request["operation_id"]))
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        custody(core)
def test_accounting_retains_erasure_and_owner_stop_semantics(core, tmp_path):
    case = migrated(core, tmp_path)
    request, private = prepared(core, tmp_path, case.request_id)
    model_accounting.record(core.store, core.settings, request, private, core.clock)
    original = core.store.one("SELECT event_id FROM jobs WHERE job_id=?", (case.job_id,))
    with core.store.transaction():
        core.ledger.expire_events(core.store, [original["event_id"]], core.clock())
    assert core.store.one("SELECT redacted FROM events WHERE event_id=?",
                          (original["event_id"],))["redacted"] == 1
    assert len(custody(core)["model_accounting"]) == 1
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="stop-after-accounting", authority=case.successor["authority"]))
    before = snapshot(core)
    assert model_accounting.record(core.store, core.settings, request, private, core.clock)[
        "billing_status"] == "ESTIMATED"
    assert snapshot(core) == before
    assert owner_authority._latest(core.store)["state"] == "STOPPED"


def test_same_policy_activation_keeps_repriced_original_liability(core, tmp_path):
    case = migrated(core, tmp_path)
    request, private = prepared(core, tmp_path, case.request_id)
    result = model_accounting.record(core.store, core.settings, request, private, core.clock)
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="stop-repriced-carry", authority=case.successor["authority"]))
    activate(core, case.policy, revision=4, operation="resume-repriced-carry")
    assert core.store.one("SELECT billed_micro_usd,state FROM model_usage") == {
        "billed_micro_usd": result["accounted_micro_usd"], "state": "UNKNOWN"}


@pytest.mark.parametrize("fresh_unknown", [False, True])
def test_new_approval_renewal_preserves_accounting_and_original_closure(
    core, tmp_path, fresh_unknown,
):
    case = migrated(core, tmp_path)
    target = current_unknown(core) if fresh_unknown else case.request_id
    request, private = prepared(core, tmp_path, target)
    result = model_accounting.record(core.store, core.settings, request, private, core.clock)
    before = core.store.rows("SELECT * FROM model_usage ORDER BY request_id")
    value, policy, witness = renewal(core, case)
    model_carry.prepare(core.store, core.settings, value, witness, core.clock())
    activate(core, policy, revision=4, operation="activate-repriced-renewal")
    assert core.store.rows("SELECT * FROM model_usage ORDER BY request_id") == before
    view = custody(core)
    assert view["model_accounting"][request["operation_id"]]["sha256"] == result["receipt_sha256"]
    assert target in view["model_closures"]
    assert model_carry.scope_windows(view, core.settings.paid_model.window_id) == [
        "offline-window-1", "renewed-window-1", "successor-window-1"]


def test_real_cli_in_independent_process_previews_and_records_once(core, tmp_path):
    case = migrated(core, tmp_path)
    value, private = proposal(tmp_path, case.request_id)
    settings = tmp_path / "settings.pickle"
    settings.write_bytes(pickle.dumps(core.settings))
    request = tmp_path / "request.json"
    script = """import pickle,sys
from companion.config import Settings
from companion.__main__ import main
with open(sys.argv.pop(1),'rb') as stream:cfg=pickle.load(stream)
Settings.load=classmethod(lambda cls,**kwargs:cfg)
main()
"""

    def execute(command, data):
        request.write_text(encode(data), encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", "-c", script, str(settings), command,
            "--request", str(request), "--request-sha256",
            hashlib.sha256(request.read_bytes()).hexdigest(), "--evidence", str(private)],
            capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    before = snapshot(core)
    value = execute("preview-model-accounting", value)["prepared_request"]
    assert snapshot(core) == before
    first = execute("record-model-accounting", value)
    recorded = snapshot(core)
    assert execute("record-model-accounting", value) == first
    assert snapshot(core) == recorded
