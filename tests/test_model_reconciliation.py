"""Offline recovery keeps UNKNOWN, prices, usage and every independent witness."""

import asyncio
import hashlib
import json
import os
import pickle
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from companion import (
    budget,
    job_queue,
    model_carry,
    model_reconciliation,
    owner_authority,
    paid_budget,
)
from companion.contracts import CoreError
from companion.model import ModelClient
from companion.model_carry_controller import verify
from tests.model_carry_support import MESSAGES, executors, observer
from tests.owner_model_fixtures import authorized_job
from tests.support.paid_model import approved_live


def stranded(core, tmp_path, *, started=True):
    approved_live(core, tmp_path, concurrency=10, concurrency_basis="local_active_attempts_v2")
    job_id = authorized_job(core)
    messages = MESSAGES
    request_id = budget.reserve(core.store, core.settings, job_id, "chat", messages,
                                core.clock(), clock=core.clock)
    if started:
        def provider(request):
            raise httpx.ReadTimeout("synthetic interrupted worker", request=request)

        async def interrupted():
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                with pytest.raises(CoreError, match="MODEL_RETRYABLE"):
                    await budget._dispatch(ModelClient(core.settings, client), core.store,
                        core.settings, request_id, messages, "chat", core.clock, dispatch={})
        asyncio.run(interrupted())
    core.clock.advance(core.settings.lease_seconds + 1)
    # Reproduce the exact former expiry write, then its native failed settlement.
    with core.store.transaction():
        core.store.execute("UPDATE model_usage SET state='UNKNOWN' WHERE request_id=%s",
                           (request_id,))
        job_queue.release(core.store, job_id, "FAILED", core.clock(),
                          reason="WORKER_LEASE_EXPIRED")
    budget.settle(core.store, core.settings, request_id)
    items = executors(core.clock())
    stopped = verify(items, observe=observer(items))
    admission = core.store.one("SELECT * FROM model_admissions WHERE request_id=%s", (request_id,))
    assert bool(admission["io_started_at"]) is started
    usage = core.store.one("SELECT * FROM model_usage WHERE request_id=%s", (request_id,))
    rows, _ = paid_budget._journal_records(Path(core.settings.paid_model.journal_path).read_bytes())
    request = model_reconciliation.Request(version=1, operation_id="native-expired-reconciliation",
        request_id=request_id, journal_id=admission["journal_id"],
        expected_head_sha256=rows[-1]["sha256"], admission_sha256=paid_budget.sha(admission),
        usage_sha256=paid_budget.sha(usage), authority_sha256=paid_budget.sha(
            owner_authority.reference(owner_authority._latest(core.store)).as_dict()),
        authorization_sha256="a" * 64, executors=items)
    return request, stopped, admission, usage


def reconcile(core, request, stopped, *, commit=False):
    return model_reconciliation.reconcile(core.store, core.settings, request, stopped,
                                          core.clock(), commit=commit)


@pytest.mark.parametrize("started", [False, True])
def test_offline_completion_and_retry_preserve_all_financial_rows(core, tmp_path, started):
    request, stopped, admission, usage = stranded(core, tmp_path, started=started)
    path = Path(core.settings.paid_model.journal_path)
    original = path.read_bytes()
    preview = reconcile(core, request, stopped)
    assert preview["state"] == "PREVIEWED_OFFLINE" and path.read_bytes() == original
    assert core.store.one("SELECT value FROM runtime_meta WHERE key='model_budget_blocked'")
    result = reconcile(core, request, stopped, commit=True)
    assert result["state"] == "RECONCILED_OFFLINE" and path.read_bytes().startswith(original)
    after = path.read_bytes()
    assert reconcile(core, request, stopped, commit=True) == result
    assert path.read_bytes() == after
    assert core.store.one("SELECT * FROM model_admissions WHERE request_id=%s",
                          (request.request_id,)) == admission
    assert core.store.one("SELECT * FROM model_usage WHERE request_id=%s",
                          (request.request_id,)) == usage
    assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] > 0
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")
    view = paid_budget.journal(core.store, core.settings.paid_model, allow_settlement=True)
    assert not view["blocked_request_ids"]
    assert view["model_latest"][request.request_id]["state"] == "UNKNOWN"
    assert view["model_closures"][request.request_id] == result["execution_closure_sha256"]
    audit = core.store.rows("SELECT * FROM audit_log WHERE action=%s",
                           (model_reconciliation.ACTION,))
    assert len(audit) == 1
    assert json.loads(audit[0]["safe_detail_json"])["authorization_sha256"] == "a" * 64
    assert not core.store.rows("SELECT * FROM outbox")
    assert core.claim_job("after-accounting-repair", job_id=usage["job_id"]) is None


@pytest.mark.parametrize("change", ["usage", "admission", "head", "restore", "live_job",
                                   "marker", "unverified", "corrupt_chain"])
def test_changed_evidence_cannot_complete_or_clear_a_block(core, tmp_path, change):
    request, stopped, _, _ = stranded(core, tmp_path)
    path = Path(core.settings.paid_model.journal_path)
    if change == "usage":
        core.store.execute("UPDATE model_usage SET billed_micro_usd=billed_micro_usd+1 "
                           "WHERE request_id=%s", (request.request_id,))
    elif change == "admission":
        core.store.execute("UPDATE model_admissions SET input_bound=input_bound+1 "
                           "WHERE request_id=%s", (request.request_id,))
    elif change == "head":
        request = request.model_copy(update={"expected_head_sha256": "b" * 64})
    elif change == "restore":
        core.store.execute("UPDATE runtime_meta SET value='changed-generation' "
                           "WHERE key='owner_authority_restore_generation'")
    elif change == "live_job":
        core.store.execute("UPDATE jobs SET state='RUNNING' WHERE job_id IN "
                           "(SELECT job_id FROM model_usage WHERE request_id=%s)",
                           (request.request_id,))
    elif change == "marker":
        core.store.execute("INSERT INTO runtime_meta(key,value) "
                           "VALUES('model_budget_restore_required','1')")
    elif change == "unverified":
        stopped = request.executors
    else:
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b'"kind":"header"', b'"kind":"HEADER"', 1))
    before = path.read_bytes()
    with pytest.raises(CoreError):
        reconcile(core, request, stopped, commit=True)
    assert path.read_bytes() == before
    assert core.store.one("SELECT value FROM runtime_meta WHERE key='model_budget_blocked'")
    assert not core.store.rows("SELECT * FROM audit_log WHERE action=%s",
                               (model_reconciliation.ACTION,))


def test_fsync_before_projection_commit_can_finish_exact_same_request(core, tmp_path, monkeypatch):
    request, stopped, _, _ = stranded(core, tmp_path)
    original = core.store.audit

    def fail_audit(action, *args, **kwargs):
        if action == model_reconciliation.ACTION:
            raise OSError("synthetic failure after original journal fsync")
        return original(action, *args, **kwargs)

    monkeypatch.setattr(core.store, "audit", fail_audit)
    with pytest.raises(CoreError):
        reconcile(core, request, stopped, commit=True)
    path = Path(core.settings.paid_model.journal_path)
    after_fsync = path.read_bytes()
    assert core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")
    monkeypatch.setattr(core.store, "audit", original)
    reconcile(core, request, stopped, commit=True)
    assert path.read_bytes() == after_fsync
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")


def test_independent_block_is_never_retired_by_expiry_reconciliation(core, tmp_path):
    request, stopped, admission, usage = stranded(core, tmp_path)
    record = {"kind": "blocked", "state": "UNKNOWN", "request_id": request.request_id,
              "minimum_micro_usd": usage["billed_micro_usd"],
              "contract_sha256": admission["contract_sha256"],
              "io_started_at": admission["io_started_at"]}
    with core.store.transaction():
        view = paid_budget.journal(core.store, core.settings.paid_model, record,
                                    allow_settlement=True)
    request = request.model_copy(update={"expected_head_sha256": view["head_sha256"]})
    path = Path(core.settings.paid_model.journal_path)
    before = path.read_bytes()
    with pytest.raises(CoreError):
        reconcile(core, request, stopped, commit=True)
    assert path.read_bytes() == before
    assert core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")


def test_failure_between_settlement_and_execution_close_resumes_only_missing_append(
    core, tmp_path, monkeypatch,
):
    request, stopped, _, _ = stranded(core, tmp_path)
    native = paid_budget.journal

    def fail_close(store, approval, record=None, **kwargs):
        if record and record.get("kind") == model_carry.CLOSE:
            raise OSError("synthetic failure between native journal appends")
        return native(store, approval, record, **kwargs)

    monkeypatch.setattr(paid_budget, "journal", fail_close)
    with pytest.raises(CoreError):
        reconcile(core, request, stopped, commit=True)
    assert core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")
    monkeypatch.setattr(paid_budget, "journal", native)
    reconcile(core, request, stopped, commit=True)
    rows, _ = paid_budget._journal_records(Path(core.settings.paid_model.journal_path).read_bytes())
    kinds = [r["kind"] for r in rows if r.get("request_id") == request.request_id]
    assert kinds.count("settle") == kinds.count(model_carry.CLOSE) == 1
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")


def test_changed_operation_cannot_replay_a_completed_request(core, tmp_path):
    request, stopped, _, _ = stranded(core, tmp_path)
    reconcile(core, request, stopped, commit=True)
    path = Path(core.settings.paid_model.journal_path)
    before = path.read_bytes()
    changed = request.model_copy(update={"operation_id": "different-reconciliation-operation"})
    with pytest.raises(CoreError):
        reconcile(core, changed, stopped, commit=True)
    assert path.read_bytes() == before
    assert len(core.store.rows("SELECT * FROM audit_log WHERE action=%s",
                               (model_reconciliation.ACTION,))) == 1


def test_real_cli_preview_and_commit_keep_unknown_without_dispatch(core, tmp_path):
    request, _, admission, usage = stranded(core, tmp_path)
    configuration = tmp_path / "synthetic-settings.pickle"
    configuration.write_bytes(pickle.dumps(core.settings))
    body = tmp_path / "reconcile.json"
    body.write_text(request.model_dump_json(), encoding="utf8")
    command = [sys.executable, str(Path(__file__).with_name("expired_accounting_cli.py")),
               str(configuration), "reconcile-expired-model-attempt", "--request", str(body),
               "--request-sha256", hashlib.sha256(body.read_bytes()).hexdigest()]
    path = Path(core.settings.paid_model.journal_path)
    before = path.read_bytes()
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for arguments, state in [(command, "PREVIEWED_OFFLINE"),
                             ([*command, "--commit"], "RECONCILED_OFFLINE")]:
        result = subprocess.run(arguments, capture_output=True, timeout=40, env=environment)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert json.loads(result.stdout)["state"] == state
        if state == "PREVIEWED_OFFLINE":
            assert path.read_bytes() == before
    assert core.store.one("SELECT * FROM model_admissions WHERE request_id=%s",
                          (request.request_id,)) == admission
    assert core.store.one("SELECT * FROM model_usage WHERE request_id=%s",
                          (request.request_id,)) == usage
    assert not core.store.rows("SELECT * FROM outbox")
