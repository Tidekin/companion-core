"""Independent native workers cannot claim the same business task twice."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from companion import budget
from companion.core import Core
from companion.goals import GoalProposal, GoalScope
from companion.store import after
from tests.conftest import ingest

DAY = date(2026, 10, 18)


def prepare(core, kind):
    core.settings.diary_enabled = core.settings.consolidation_enabled = True
    core.settings.reflection_enabled = True
    core.settings.reflection_daily_micro_usd = 50000
    core.settings.reflection_monthly_micro_usd = 100000
    ingest(core, at=DAY.isoformat() + "T04:00:00+00:00")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE event_id = %s", ("evt_test_1",))
    if kind == "diary":
        return core.diary.schedule_day(DAY)
    if kind == "consolidation":
        return core.consolidation.schedule_day(DAY)
    if kind == "goal":
        core.settings.goals_enabled = core.settings.exploration_enabled = True
        proposal = GoalProposal(topic="合成阅读任务", source_event_ids=["evt_test_1"],
                                scope=GoalScope(operation="read_content",
                                                url="https://example.org/article"))
        goal_id = core.goals.propose(proposal)
        core.goals.accept(goal_id)
        return goal_id


def claim(worker, kind):
    if kind == "diary":
        return worker.diary.repo.claim("native-business-worker")
    if kind == "consolidation":
        return worker.consolidation.claim()
    if kind == "reflection":
        return worker.reflections.claim(DAY)
    return worker.goals.claim()


def interleaved(core, monkeypatch, selected_query, operation):
    entered, release = threading.Event(), threading.Event()
    original = core.store.one

    def select_then_pause(sql, params=()):
        value = original(sql, params)
        if selected_query in sql and not entered.is_set():
            entered.set()
            assert release.wait(15)
        return value

    with Core(core.settings, core.model, core.clock) as other, ThreadPoolExecutor(2) as pool:
        monkeypatch.setattr(core.store, "one", select_then_pause)
        first = pool.submit(operation, core)
        try:
            assert entered.wait(10)
            second = pool.submit(operation, other).result(timeout=10)
        finally:
            release.set()
        return first.result(timeout=10), second


@pytest.mark.parametrize(("kind", "selected_query"), [
    ("diary", "WHERE diary_runs.state IN ('PENDING', 'RETRY_WAIT')"),
    ("consolidation", "consolidation_runs.timezone = %s AND ("),
    ("goal", "FROM goal_steps AS st JOIN goals AS g"),
    ("reflection", "WHERE reflection_runs.local_date = %s AND "),
])
def test_independent_workers_claim_one_business_task(core, monkeypatch, kind, selected_query):
    prepare(core, kind)
    first_run, second_run = interleaved(
        core, monkeypatch, selected_query, lambda worker: claim(worker, kind),
    )
    claimed = [row for row in (first_run, second_run) if row]
    assert len(claimed) == 1
    assert claimed[0]["lease_token"]
    assert core.store.one("SELECT count(*) AS n FROM model_usage")["n"] == 0
    assert core.store.one("SELECT count(*) AS n FROM outbox")["n"] == 0


@pytest.mark.parametrize(("kind", "selected_query"), [
    ("diary", "SELECT run_id FROM diary_runs WHERE identity_id"),
    ("consolidation", "WHERE consolidation_runs.local_date = %s"),
])
def test_two_schedulers_return_the_same_durable_run(core, monkeypatch, kind, selected_query):
    core.settings.diary_enabled = core.settings.consolidation_enabled = True
    first, second = interleaved(
        core, monkeypatch, selected_query, lambda worker: getattr(worker, kind).schedule_day(DAY),
    )
    assert first and first == second
    assert core.store.one(f"SELECT count(*) AS n FROM {kind}_runs")["n"] == 1


@pytest.mark.parametrize("kind", ["diary", "reflection"])
def test_expiry_takes_the_original_fee_before_the_run_and_rechecks_its_lease(core, kind):
    prepare(core, kind)
    old = claim(core, kind)
    original = budget.reserve(core.store, core.settings, None, kind, [], core.clock())
    other = budget.reserve(core.store, core.settings, None, "memory_extract", [], core.clock())
    table = f"{kind}_runs"
    core.store.execute(f"UPDATE {table} SET model_request_id=%s WHERE run_id=%s",
                       (original, old["run_id"]))
    core.clock.advance(core.settings.lease_seconds + 1)
    with Core(core.settings, core.model, core.clock) as finisher, ThreadPoolExecutor(1) as pool:
        with finisher.store.transaction():
            finisher.store.one("SELECT request_id FROM model_usage WHERE request_id=%s FOR UPDATE",
                               (original,))
            recovery = pool.submit(claim, core, kind)
            deadline = time.monotonic() + 10
            while not finisher.store.one("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s)) "
                                         "AS waiting", (core.store.backend_pid,))["waiting"]:
                assert not recovery.done() and time.monotonic() < deadline
                time.sleep(0.01)
            # A writer owning the financial row can still lock its run and renew it.
            finisher.store.one(f"SELECT run_id FROM {table} WHERE run_id=%s FOR UPDATE NOWAIT",
                               (old["run_id"],))
            finisher.store.execute(
                f"UPDATE {table} SET lease_until=%s, lease_token='renewed' WHERE run_id=%s",
                (after(core.clock(), 60), old["run_id"]),
            )
        assert recovery.result(timeout=10) is None
    assert core.store.one(f"SELECT state,lease_token FROM {table} WHERE run_id=%s",
                          (old["run_id"],)) == {"state": "RUNNING", "lease_token": "renewed"}
    assert core.store.one("SELECT state FROM model_usage WHERE request_id=%s",
                          (original,))["state"] == "RESERVED"

    core.clock.advance(61)
    assert claim(core, kind) is None
    assert core.store.one("SELECT state FROM model_usage WHERE request_id=%s",
                          (original,))["state"] == "UNKNOWN"
    assert core.store.one("SELECT state FROM model_usage WHERE request_id=%s",
                          (other,))["state"] == "RESERVED"
    assert core.store.one(f"SELECT state FROM {table} WHERE run_id=%s",
                          (old["run_id"],))["state"] == "FAILED"
