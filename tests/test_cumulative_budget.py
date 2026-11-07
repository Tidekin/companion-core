"""Offline cumulative liability regressions; never contact a provider or QQ."""

import asyncio
import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from companion import budget, cumulative, paid_budget
from companion.contracts import CoreError
from companion.model import ModelClient, ModelResponse
from tests.owner_model_fixtures import authorized_job
from tests.support.cumulative_budget import enable, policy, view
from tests.support.paid_model import approved_live

MESSAGES = [{"role": "user", "content": "hello"}]


def reserve(core, **kwargs):
    return budget.reserve(core.store, core.settings, authorized_job(core),
                          "chat", MESSAGES, core.clock(),
                          clock=core.clock, **kwargs)


@pytest.mark.parametrize("period", ["request", "window", "day", "month", "account"])
@pytest.mark.parametrize("dimension", [
    "micro_usd", "model_attempts", "input_tokens", "output_tokens",
])
def test_all_model_limits_apply_before_reservation(core, tmp_path, period, dimension):
    enable(core, tmp_path, **{period: {dimension: 0}})
    with pytest.raises(CoreError, match="CUMULATIVE_LIMIT_EXHAUSTED"):
        reserve(core)
    assert core.store.rows("SELECT * FROM model_usage") == []
    assert core.model.calls == []


@pytest.mark.parametrize("boundary", ["2026-10-19T15:59:59+00:00", "2026-10-31T15:59:59+00:00"])
def test_local_midnight_changes_period_but_preserves_account(core, tmp_path, boundary):
    core.clock.now = datetime.fromisoformat(boundary)
    enable(core, tmp_path, lease_seconds=2 * 86400,
           day={"model_attempts": 1}, account={"model_attempts": 2})
    first = reserve(core)
    budget.settle(core.store, core.settings, first, ModelResponse("ok", 30, 2))
    with pytest.raises(CoreError, match="CUMULATIVE_LIMIT_EXHAUSTED"):
        reserve(core)
    core.clock.advance(2)
    second = reserve(core)
    budget.settle(core.store, core.settings, second, ModelResponse("ok", 30, 2))
    core.clock.advance(86400)
    with pytest.raises(CoreError, match="CUMULATIVE_LIMIT_EXHAUSTED"):
        reserve(core)
    assert len(view(core)["models"]) == 2


def test_rotation_and_new_window_do_not_reset_prior_account_calls(core, tmp_path):
    enable(core, tmp_path, account={"model_attempts": 1})
    first = reserve(core)
    budget.settle(core.store, core.settings, first, ModelResponse("ok", 30, 2))
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (authorized_job(core),))
    cumulative.pause(core.store, core.settings.paid_model, core.clock())
    core.settings.model_key = SecretStr("synthetic-rotated-key")
    core.settings.paid_model = core.settings.paid_model.model_copy(update={"window_id": "second"})
    policy(core, account={"model_attempts": 1}, key_ownership_sha256="1" * 64)
    with core.store.transaction():
        cumulative.activate(core.store, core.settings, core.clock())
    authorized_job(core, renew=True)
    with pytest.raises(CoreError, match="CUMULATIVE_LIMIT_EXHAUSTED"):
        reserve(core)
    assert len(view(core)["models"]) == 1
    assert len(view(core)["policies"]) == 2


@pytest.mark.parametrize("change", ["account", "timezone", "rotation_proof", "downgrade"])
def test_account_binding_cannot_be_silently_replaced(core, tmp_path, change):
    enable(core, tmp_path)
    if change == "downgrade":
        core.settings.paid_model = core.settings.paid_model.model_copy(update={"cumulative": None})
    elif change == "rotation_proof":
        core.settings.model_key = SecretStr("synthetic-rotated-key")
        policy(core)
    else:
        policy(core, **({"account_ref": "2" * 64,
                        "credential_sha256": cumulative.credential(core.settings, "2" * 64)}
                       if change == "account" else {"timezone": "UTC"}))
    with pytest.raises(CoreError):
        reserve(core)
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_unknown_and_late_usage_keep_original_period_and_never_refund(core, tmp_path):
    core.clock.now = datetime.fromisoformat("2026-10-31T15:59:59+00:00")
    enable(core, tmp_path)
    request = reserve(core)
    before = dict(view(core)["models"][request])
    budget.settle(core.store, core.settings, request)
    core.clock.advance(2)
    budget.settle(core.store, core.settings, request, ModelResponse("ok", 30, 2))
    after = view(core)["models"][request]
    assert after["at"] == before["at"]
    assert after["state"] == "UNKNOWN"
    assert all(after[k] >= before[k] for k in cumulative.DIMENSIONS)
    with pytest.raises(CoreError):
        reserve(core)


def test_actual_token_overrun_is_unknown_and_stops_new_work(core, tmp_path):
    estimate = paid_budget.estimate_input_tokens(approved_live(core, tmp_path), MESSAGES, "chat")
    policy(core, day={"input_tokens": estimate + 1})
    request = reserve(core)
    budget.settle(core.store, core.settings, request, ModelResponse("ok", estimate + 2, 2))
    assert view(core)["models"][request]["state"] == "UNKNOWN"
    assert view(core)["paused"]
    with pytest.raises(CoreError):
        reserve(core)


def test_clock_rollback_pause_survives_transaction_rollback(core, tmp_path):
    enable(core, tmp_path)
    core.clock.advance(-1)
    with pytest.raises(CoreError, match="CLOCK_ROLLBACK"):
        reserve(core)
    assert view(core)["reason"] == "CLOCK_ROLLBACK"
    core.clock.advance(2)
    with pytest.raises(CoreError, match="CUMULATIVE_PAUSED"):
        reserve(core)


def test_legacy_costs_require_exact_inventory_and_are_not_reset(core, tmp_path):
    approved_live(core, tmp_path)
    request = reserve(core)
    budget.settle(core.store, core.settings, request, ModelResponse("ok", 30, 2))
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (authorized_job(core),))
    core.settings.paid_model = core.settings.paid_model.model_copy(update={"window_id": "new"})
    policy(core, account={"model_attempts": 1})
    with core.store.transaction():
        cumulative.activate(core.store, core.settings, core.clock())
    authorized_job(core, renew=True)
    with pytest.raises(CoreError, match="CUMULATIVE_LIMIT_EXHAUSTED"):
        reserve(core)
    assert view(core)["models"][request]["micro_usd"] == 34


def test_policy_and_reservation_fsync_crashes_require_reconciliation(core, tmp_path, monkeypatch):
    enable(core, tmp_path)
    original = paid_budget.journal

    def crash(store, approval, record=None, **kwargs):
        value = original(store, approval, record, **kwargs)
        if record and record["kind"] == "reserve":
            raise SystemExit("synthetic process exit after fsync")
        return value

    monkeypatch.setattr(paid_budget, "journal", crash)
    with pytest.raises(SystemExit):
        reserve(core)
    monkeypatch.setattr(paid_budget, "journal", original)
    with pytest.raises(CoreError, match="RECONCILIATION"):
        reserve(core)
    assert core.model.calls == []


def test_final_http_hook_observes_pause_before_transport(core, tmp_path):
    enable(core, tmp_path)
    request = reserve(core)
    calls = []

    async def paused(_):
        cumulative.pause(core.store, core.settings.paid_model, core.clock())

    async def exercise():
        async with httpx.AsyncClient(event_hooks={"request": [paused]},
            transport=httpx.MockTransport(lambda req: calls.append(req))) as client:
            with pytest.raises(CoreError, match="CUMULATIVE_PAUSED"):
                await budget.complete(ModelClient(core.settings, client), core.store,
                    core.settings, request, MESSAGES, "chat", core.clock)

    asyncio.run(exercise())
    assert calls == []


def test_two_processes_share_account_cap(core, tmp_path):
    import subprocess
    import sys

    enable(core, tmp_path, account={"model_attempts": 1})
    script = tmp_path / "quota_reserve.py"
    script.write_text("import sys\nfrom tests.process_helpers import reservation_process\n"
                      "reservation_process(*sys.argv[1:])\n", encoding="utf-8")
    config = core.settings.model_dump(mode="json")
    config["model_key"] = core.settings.model_key.get_secret_value()
    command = [sys.executable, str(script), core.settings.database,
               json.dumps(config), core.clock(), authorized_job(core)]
    processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for _ in range(2)]
    results = [process.communicate(timeout=40) for process in processes]
    assert all(process.returncode == 23 for process in processes), results
    outputs = [out.strip() for out, _ in results]
    assert sum(value.startswith("model_") for value in outputs) == 1
    assert outputs.count("CUMULATIVE_LIMIT_EXHAUSTED") == 1


def test_legacy_snapshots_do_not_gain_empty_policy(core, tmp_path):
    approval = approved_live(core, tmp_path)
    assert "cumulative" not in approval.model_dump(mode="json")


def test_committed_pause_cannot_be_removed_by_a_valid_journal_prefix(core, tmp_path):
    enable(core, tmp_path)
    path = Path(core.settings.paid_model.journal_path)
    prior = path.read_bytes()
    cumulative.pause(core.store, core.settings.paid_model, core.clock())
    path.write_bytes(prior)
    with pytest.raises(CoreError, match="RECONCILIATION"):
        reserve(core)
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_expiry_is_durable_even_when_old_window_validator_would_fail_first(core, tmp_path):
    enable(core, tmp_path)
    core.clock.now = datetime.fromisoformat("2026-12-01T00:00:01+08:00")
    with pytest.raises(CoreError, match="EXPIRED"):
        reserve(core)
    assert view(core)["reason"] == "EXPIRED"
    core.clock.now = datetime.fromisoformat("2026-10-20T04:00:00+00:00")
    with pytest.raises(CoreError):
        reserve(core)
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_reserved_model_cannot_dispatch_in_another_natural_day(core, tmp_path):
    core.clock.now = datetime.fromisoformat("2026-10-31T15:59:59+00:00")
    enable(core, tmp_path)
    request = reserve(core)
    calls = []

    async def midnight(_):
        core.clock.advance(2)

    async def exercise():
        async with httpx.AsyncClient(event_hooks={"request": [midnight]},
            transport=httpx.MockTransport(lambda req: calls.append(req))) as client:
            with pytest.raises(CoreError, match="CUMULATIVE_DISPATCH_PERIOD_CHANGED"):
                await budget.complete(ModelClient(core.settings, client), core.store,
                    core.settings, request, MESSAGES, "chat", core.clock)

    asyncio.run(exercise())
    assert calls == []


@pytest.mark.parametrize("missing", [False, True])
def test_old_database_and_latest_or_missing_journal_never_replenish(core, tmp_path, missing):
    import sqlite3
    from contextlib import closing

    from companion.store import Store

    enable(core, tmp_path)
    old = tmp_path / "old.db"
    with closing(sqlite3.connect(old)) as destination:
        core.store.db.backup(destination)
    request = reserve(core)
    budget.settle(core.store, core.settings, request, ModelResponse("ok", 30, 2))
    if missing:
        Path(core.settings.paid_model.journal_path).unlink()
    restored = Store(str(old))
    try:
        with pytest.raises(CoreError, match="RECONCILIATION"):
            budget.reserve(restored, core.settings, authorized_job(core),
                           "chat", MESSAGES, core.clock())
    finally:
        restored.close()
    assert core.model.calls == []


def test_disconnect_keeps_liability_and_cannot_trigger_a_second_paid_request(core, tmp_path):
    enable(core, tmp_path)
    request = reserve(core)
    calls = []

    def disconnect(request):
        calls.append(request)
        raise httpx.ReadError("synthetic disconnect")

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(disconnect)) as client:
            with pytest.raises(CoreError, match="MODEL_RETRYABLE"):
                await budget.complete(ModelClient(core.settings, client), core.store,
                    core.settings, request, MESSAGES, "chat", core.clock)

    asyncio.run(exercise())
    # The existing worker owns settlement after transport exceptions.
    budget.settle(core.store, core.settings, request)
    assert view(core)["models"][request]["state"] == "UNKNOWN"
    with pytest.raises(CoreError):
        reserve(core)
    assert len(calls) == 1


def test_vision_and_chat_consume_the_same_cumulative_model_attempts(core, tmp_path):
    from tests.support.paid_image_budget import image_approval, reserve_image

    image_approval(core, tmp_path)
    policy(core, account={"model_attempts": 1})
    job, request = reserve_image(core)
    budget.settle(core.store, core.settings, request, ModelResponse("ok", 1000, 20))
    with pytest.raises(CoreError, match="CUMULATIVE_LIMIT_EXHAUSTED"):
        budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock(),
                       logical_request_id=job)
    assert len(view(core)["models"]) == 1 and core.model.calls == []


@pytest.mark.parametrize("outer_transaction", [False, True])
def test_automatic_pause_witness_survives_business_rollback_and_prefix_replay(
    core, tmp_path, outer_transaction,
):
    enable(core, tmp_path)
    path = Path(core.settings.paid_model.journal_path)
    prefix = path.read_bytes()
    core.clock.now = datetime.fromisoformat("2026-12-01T00:00:01+08:00")
    with pytest.raises(CoreError, match="EXPIRED"):
        if outer_transaction:
            with core.store.transaction():
                core.store.execute('INSERT INTO runtime_meta VALUES (%s,%s)',
                                   ("uncommitted_test", "business"))
                cumulative.preflight(core.store, core.settings, core.clock)
        else:
            reserve(core)
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key='uncommitted_test'")
    assert core.store.one("SELECT 1 FROM runtime_meta WHERE key LIKE 'model_budget_stop:%%'")
    assert view(core)["paused"]
    path.write_bytes(prefix)
    core.clock.now = datetime.fromisoformat("2026-10-20T04:00:00+00:00")
    with pytest.raises(CoreError, match="RECONCILIATION"):
        reserve(core)
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_missing_journal_and_identity_status_does_not_bootstrap_or_modify_anything(core, tmp_path):
    enable(core, tmp_path)
    path = Path(core.settings.paid_model.journal_path)
    path.unlink()
    core.store.execute("DELETE FROM runtime_meta WHERE key='model_budget_journal_id'")
    before = list(core.store.db.iterdump())
    result = cumulative.status(core.store, core.settings, core.clock())
    assert result == {"enrolled": True, "available": False,
                      "code": "MODEL_BUDGET_RECONCILIATION_REQUIRED"}
    assert list(core.store.db.iterdump()) == before
    assert not path.exists()


def test_pause_fsync_then_projection_failure_retains_only_the_stop_witness(
    core, tmp_path, monkeypatch,
):
    enable(core, tmp_path)
    path = Path(core.settings.paid_model.journal_path)
    prefix = path.read_bytes()
    execute = core.store.execute

    def fail_projection(sql, params=()):
        if sql.startswith("INSERT OR REPLACE INTO runtime_meta") and params[0] == (
            cumulative.PROJECTION
        ):
            raise RuntimeError("synthetic pause projection failure after fsync")
        return execute(sql, params)

    monkeypatch.setattr(core.store, "execute", fail_projection)
    with pytest.raises(RuntimeError, match="after fsync"):
        with core.store.transaction():
            core.store.execute('INSERT INTO runtime_meta VALUES (%s,%s)', ("business", "rollback"))
            cumulative.pause(core.store, core.settings.paid_model, core.clock())
    monkeypatch.setattr(core.store, "execute", execute)
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key='business'")
    assert view(core)["paused"]
    path.write_bytes(prefix)
    with pytest.raises(CoreError, match="RECONCILIATION"):
        reserve(core)


@pytest.mark.parametrize("projection", ["invalid", "{}", "[]", '{"journal_path":null}'])
def test_damaged_projection_status_remains_read_only_and_reports_unavailable(
    core, tmp_path, projection,
):
    enable(core, tmp_path)
    core.store.execute('UPDATE runtime_meta SET value=%s WHERE key=%s',
                       (projection, cumulative.PROJECTION))
    before = list(core.store.db.iterdump())
    journal = Path(core.settings.paid_model.journal_path).read_bytes()
    result = cumulative.status(core.store, core.settings, core.clock())
    assert not result["available"] and result["code"] == "MODEL_BUDGET_RECONCILIATION_REQUIRED"
    assert list(core.store.db.iterdump()) == before
    assert Path(core.settings.paid_model.journal_path).read_bytes() == journal


@pytest.mark.parametrize("removed", ["paid_approval", "cumulative_scope"])
def test_final_http_hook_cannot_remove_enrolled_cumulative_authority(core, tmp_path, removed):
    enable(core, tmp_path)
    approval = core.settings.paid_model
    request = reserve(core)
    calls = []

    async def remove(_):
        core.settings.paid_model = (None if removed == "paid_approval" else
                                    approval.model_copy(update={"cumulative": None}))

    async def exercise():
        async with httpx.AsyncClient(event_hooks={"request": [remove]},
            transport=httpx.MockTransport(lambda req: calls.append(req))) as client:
            with pytest.raises(CoreError, match="CUMULATIVE_SCOPE_REQUIRED"):
                await budget.complete(ModelClient(core.settings, client), core.store,
                    core.settings, request, MESSAGES, "chat", core.clock)
        budget.settle(core.store, core.settings, request)

    asyncio.run(exercise())
    assert calls == []
    state = paid_budget.journal(core.store, approval, allow_settlement=True)["cumulative"]
    assert state["models"][request]["state"] == "SETTLED"
    assert state["models"][request]["micro_usd"] == 0
    assert core.store.one("SELECT 1 FROM audit_log WHERE action='model.not_dispatched'")
    with pytest.raises(CoreError, match="CUMULATIVE_SCOPE_REQUIRED"):
        reserve(core)
