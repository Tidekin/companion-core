"""Recover planning metadata without repeating an admitted model or account action."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from threading import Barrier

import pytest
from pydantic import SecretStr

from companion import budget, social_plan_jobs
from companion.cloud_budget import CloudBudgetConfig
from companion.contracts import CoreError
from companion.social_autonomy_runtime import _expire, run_once
from companion.store import Store
from tests.conftest import ingest, run
from tests.support.social_autonomy import MockPlanner, background, extra_public, planned
from tests.support.social_owner_authority import authorize


def crash(core, tmp_path, ref, mode):
    config = tmp_path / "synthetic-social-crash.json"
    config.write_text(json.dumps({"settings": core.settings.model_dump(mode="python"),
                                 "source_id": ref, "now": core.clock(), "mode": mode},
                                default=lambda value: value.get_secret_value()
                                if isinstance(value, SecretStr) else str(value)), encoding="utf-8")
    child = subprocess.run([sys.executable, "-B", "-m", "tests.social_recovery_process",
                            str(config)], capture_output=True, timeout=20)
    assert child.returncode == 23, child.stderr.decode(errors="replace")


@pytest.mark.parametrize("mode", ["before_reserve", "settled", "reserved"])
def test_actual_process_death_retires_only_expired_known_planning(core, tmp_path, mode):
    module, ref, _ = background(core, plan_ttl_seconds=30)
    crash(core, tmp_path, ref, mode)
    prior = core.store.one("SELECT * FROM social_autonomy_plans")
    usage = core.store.rows("SELECT * FROM model_usage")
    assert not _expire(core.store, core.clock())
    assert core.store.one("SELECT * FROM social_autonomy_plans") == prior
    core.clock.advance(86400)
    with closing(Store(core.settings.database)) as restarted:
        assert run(run_once(restarted, core.settings, None, core.clock)) == (mode != "reserved")
    assert core.store.rows("SELECT * FROM model_usage") == usage
    assert not core.store.rows("SELECT * FROM outbox")
    if mode == "reserved":
        assert core.store.one("SELECT state FROM social_autonomy_plans")["state"] == "PLANNING"
        assert usage[0]["state"] == "RESERVED"
        return
    assert core.store.one("SELECT state,last_error_code FROM social_autonomy_plans") == {
        "state": "CANCELLED", "last_error_code": "SOCIAL_PLAN_RESULT_LOST"
        if mode == "settled" else "SOCIAL_PLAN_EXPIRED_UNADMITTED",
    }
    assert planned(module, ref, "crashed-social-plan")["state"] == "CANCELLED"
    assert not module.model.calls
    other = extra_public(core)
    assert planned(module, other, "fresh-social-plan")["state"] == "READY"
    assert len(module.model.calls) == 1
    assert len(core.store.rows("SELECT * FROM model_usage")) == len(usage) + 1


def test_settled_admission_lease_is_not_retired_while_other_worker_can_finish(core, tmp_path):
    module, ref, _ = background(core, plan_ttl_seconds=1, max_output_tokens=128)
    authorize(core, tmp_path)
    crash(core, tmp_path, ref, "settled")
    usage = core.store.rows("SELECT * FROM model_usage")
    admission = core.store.one("SELECT * FROM model_admissions")
    core.clock.advance(2)
    assert admission["lease_until"] > core.clock()
    assert not _expire(core.store, core.clock())
    core.clock.advance(60)
    assert _expire(core.store, core.clock())
    assert core.store.rows("SELECT * FROM model_usage") == usage
    assert core.store.one("SELECT * FROM model_admissions") == admission
    assert core.store.one("SELECT state FROM social_autonomy_plans")["state"] == "CANCELLED"


def test_native_reserve_fences_worker_resuming_after_expired_plan_recovery(core, monkeypatch):
    module, ref, _ = background(core, plan_ttl_seconds=1)
    original = budget.reserve

    def pause_then_resume(*args, **kwargs):
        core.clock.advance(2)
        assert _expire(core.store, core.clock())
        return original(*args, **kwargs)

    monkeypatch.setattr(budget, "reserve", pause_then_resume)
    with pytest.raises(CoreError, match="SOCIAL_MODEL_SCOPE_REQUIRED"):
        planned(module, ref)
    assert not core.store.rows("SELECT * FROM model_usage")
    assert not core.store.rows("SELECT * FROM cloud_cost_snapshots")
    assert not module.model.calls
    assert core.store.one("SELECT state FROM social_autonomy_plans")["state"] == "CANCELLED"


def test_budget_refusal_reuses_original_plan_and_request_without_poll_writes(core):
    module, ref, _ = background(core)
    core.settings.cloud_budget = CloudBudgetConfig(daily_micro_cny=0)
    with pytest.raises(CoreError, match="CLOUD_DAILY_BUDGET_EXHAUSTED"):
        planned(module, ref)
    before = core.store.one("SELECT * FROM social_autonomy_plans")
    changes = core.store.db.total_changes
    assert planned(module, ref)["state"] == "REJECTED"
    # SQLite total_changes includes rolled-back writes; durable rows/audit do not change.
    assert core.store.db.total_changes >= changes
    assert core.store.one("SELECT * FROM social_autonomy_plans") == before
    assert len(core.store.rows("SELECT * FROM audit_log WHERE action='social.plan_failed'")) == 1
    assert not core.store.rows("SELECT * FROM model_usage")
    core.settings.cloud_budget = CloudBudgetConfig()
    core.clock.advance(60)
    assert run(run_once(core.store, core.settings, module.model, core.clock))
    result = module.get(before["plan_id"])
    assert result["state"] == "READY" and result["plan_id"] == before["plan_id"]
    assert result["model_request_id"] == before["model_request_id"]
    assert len(core.store.rows("SELECT * FROM social_autonomy_plans")) == 1
    assert len(core.store.rows("SELECT * FROM model_usage")) == 1
    assert len(module.model.calls) == 1


def test_native_admission_yields_to_new_chat_without_consuming_the_unadmitted_plan(
    core, monkeypatch,
):
    module, ref, _ = background(core)
    original = budget.reserve

    def interrupt(*args, **kwargs):
        ingest(core, 100, "先回答新的输入", origin="onebot-direct:FriendMessage:10001")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(budget, "reserve", interrupt)
        with pytest.raises(CoreError, match="BACKGROUND_YIELD_TO_CHAT"):
            planned(module, ref)
    before = core.store.one("SELECT * FROM social_autonomy_plans")
    assert not module.model.calls
    assert not core.store.rows("SELECT * FROM model_usage")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE kind<>'social_plan'")
    core.clock.advance(60)
    assert run(run_once(core.store, core.settings, module.model, core.clock))
    assert planned(module, ref)["plan_id"] == before["plan_id"]
    assert len(module.model.calls) == 1


def test_runtime_retries_unadmitted_candidate_across_midnight_without_extending_expiry(core):
    core.clock.now = datetime.fromisoformat("2026-10-19T15:59:50+00:00")
    module, _, _ = background(core)
    core.settings.cloud_budget = CloudBudgetConfig(daily_micro_cny=0)
    assert run(run_once(core.store, core.settings, module.model, core.clock))
    prior = core.store.one("SELECT * FROM social_autonomy_plans")
    assert prior["state"] == "REJECTED" and not module.model.calls
    assert not run(run_once(core.store, core.settings, module.model, core.clock))
    core.clock.advance(60)
    newer_ref = extra_public(core)
    core.settings.cloud_budget = CloudBudgetConfig()
    assert run(run_once(core.store, core.settings, module.model, core.clock))
    current = core.store.one("SELECT * FROM social_autonomy_plans")
    for key in ("plan_id", "model_request_id", "expires_at", "created_at", "candidate_sha256"):
        assert current[key] == prior[key]
    assert current["state"] == "READY"
    assert len(module.model.calls) == 1
    assert module.model.calls[0]["sources"][0]["ref"] != newer_ref
    assert len(core.store.rows("SELECT * FROM model_usage")) == 1
    assert core.store.one("SELECT state,attempt_count FROM outbox") == {
        "state": "PENDING", "attempt_count": 0,
    }


def test_timezone_change_is_refused_before_consuming_a_candidate_or_rebinding_old_quota(core):
    module, ref, _ = background(core)
    module.model.proposal = {"kind": "skip", "reason_code": "NO_ACTION"}
    planned(module, ref)
    usage = core.store.rows("SELECT * FROM model_usage")
    bound = core.store.one("SELECT value FROM runtime_meta WHERE key='social_autonomy_timezone'")
    core.settings.timezone = "UTC" if bound["value"] != "UTC" else "Asia/Shanghai"
    core.clock.advance(45000)
    newer_ref = extra_public(core)
    with pytest.raises(CoreError, match="SOCIAL_TIMEZONE_BINDING_CHANGED"):
        planned(module, newer_ref, "new-day-candidate")
    assert len(core.store.rows("SELECT * FROM social_autonomy_plans")) == 1
    assert core.store.rows("SELECT * FROM model_usage") == usage
    assert core.store.one(
        "SELECT value FROM runtime_meta WHERE key='social_autonomy_timezone'"
    ) == bound


@pytest.mark.parametrize("change", ["expire", "erase", "policy", "unknown"])
def test_unadmitted_retry_cannot_outlive_original_source_authority_or_liability(core, change):
    module, ref, _ = background(core, plan_ttl_seconds=300)
    core.settings.cloud_budget = CloudBudgetConfig(daily_micro_cny=0)
    with pytest.raises(CoreError, match="CLOUD_DAILY_BUDGET_EXHAUSTED"):
        planned(module, ref)
    core.settings.cloud_budget = CloudBudgetConfig()
    if change == "expire":
        core.clock.advance(301)
    elif change == "erase":
        with core.store.transaction():
            core.ledger.expire_events(core.store, [ref], core.clock())
    elif change == "policy":
        core.settings.social_autonomy = module.config.model_copy(update={"policy_version": 2})
    else:
        # A native admission, even after the earlier refusal, supersedes the no-admission proof.
        row = core.store.one("SELECT * FROM social_autonomy_plans")
        messages, _ = module._messages(json.loads(row["input_snapshot_json"]), module.config)
        core.clock.advance(60)
        job = social_plan_jobs.claim(module, "synthetic-worker")
        budget.reserve(core.store, core.settings, job["job_id"], "social_autonomy",
                       messages, core.clock(), request_id=row["model_request_id"],
                       logical_request_id="social:" + row["plan_id"], clock=core.clock,
                       source_check=lambda: module.check_retry_job(job))
        budget.settle(core.store, core.settings, row["model_request_id"], None)
    core.clock.advance(60)
    run(run_once(core.store, core.settings, module.model, core.clock))
    assert not module.model.calls
    assert not core.store.rows("SELECT * FROM outbox")
    if change == "unknown":
        before = core.store.one("SELECT * FROM model_usage")
        core.clock.advance(86400)
        _expire(core.store, core.clock())
        assert core.store.one("SELECT * FROM model_usage") == before


def test_two_connections_retry_one_original_unadmitted_request(core):
    module, ref, _ = background(core)
    core.settings.cloud_budget = CloudBudgetConfig(daily_micro_cny=0)
    with pytest.raises(CoreError, match="CLOUD_DAILY_BUDGET_EXHAUSTED"):
        planned(module, ref)
    core.settings.cloud_budget = CloudBudgetConfig()
    core.clock.advance(60)
    barrier = Barrier(2)
    calls = []

    def attempt(_):
        with closing(Store(core.settings.database)) as store:
            model = MockPlanner()
            model.callback = lambda: calls.append(True)
            barrier.wait(timeout=5)
            try:
                return run(run_once(store, core.settings, model, core.clock, worker_id="racer"))
            except CoreError as error:
                return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        states = list(workers.map(attempt, range(2)))
    assert True in states
    assert len(calls) == 1
    assert core.store.one("SELECT state FROM social_autonomy_plans")["state"] == "READY"
    assert len(core.store.rows("SELECT * FROM model_usage")) == 1


@pytest.mark.parametrize("mode", ["before_reserve", "reserved", "settled"])
def test_native_retry_process_death_reclaims_only_unadmitted_job(core, tmp_path, mode):
    module, ref, _ = background(core, plan_ttl_seconds=600)
    core.settings.cloud_budget = CloudBudgetConfig(daily_micro_cny=0)
    with pytest.raises(CoreError, match="CLOUD_DAILY_BUDGET_EXHAUSTED"):
        planned(module, ref)
    core.settings.cloud_budget = CloudBudgetConfig()
    original = core.store.one("SELECT * FROM social_autonomy_plans")
    core.clock.advance(60)
    crash(core, tmp_path, ref, "retry_" + mode)
    job = social_plan_jobs.get(core.store, original["plan_id"])
    assert job["state"] == "RUNNING"
    usage = core.store.rows("SELECT * FROM model_usage")
    assert not _expire(core.store, core.clock())
    core.clock.advance(core.settings.lease_seconds + 1)
    assert _expire(core.store, core.clock())
    recovered = social_plan_jobs.get(core.store, original["plan_id"])
    assert recovered["state"] == ("RETRY_WAIT" if mode == "before_reserve" else "CANCELLED")
    assert core.store.rows("SELECT * FROM model_usage") == usage
    core.clock.advance(60)
    run(run_once(core.store, core.settings, module.model, core.clock))
    assert len(module.model.calls) == (1 if mode == "before_reserve" else 0)
    assert core.store.one("SELECT model_request_id FROM social_autonomy_plans")[
        "model_request_id"] == original["model_request_id"]


def test_native_retry_wait_is_durable_and_not_polled_as_a_new_attempt(core):
    module, ref, _ = background(core, plan_ttl_seconds=600)
    core.settings.cloud_budget = CloudBudgetConfig(daily_micro_cny=0)
    with pytest.raises(CoreError, match="CLOUD_DAILY_BUDGET_EXHAUSTED"):
        planned(module, ref)
    plan = core.store.one("SELECT * FROM social_autonomy_plans")
    core.clock.advance(60)
    assert run(run_once(core.store, core.settings, module.model, core.clock))
    job = social_plan_jobs.get(core.store, plan["plan_id"])
    assert job["state"] == "RETRY_WAIT" and job["attempts"] == 0
    for _ in range(3):
        run(run_once(core.store, core.settings, module.model, core.clock))
    assert social_plan_jobs.get(core.store, plan["plan_id"]) == job
    assert not module.model.calls
    assert not core.store.rows("SELECT * FROM model_usage")
    assert len(core.store.rows("SELECT * FROM social_autonomy_jobs")) == 1
