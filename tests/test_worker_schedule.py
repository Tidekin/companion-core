"""Shared boundaries preserve native recovery, custody and one-shot execution."""

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest

from companion import worker_deadlines
from companion.store import after
from companion.worker_schedule import UnitResult, run_native_unit
from companion.worker_wakeup import Wakeup
from tests.conftest import run
from tests.support.executive import FakeExecutor, configured, proposal


def test_scan_context_is_immutable_and_cannot_be_reused_after_another_scan(core):
    with Wakeup(core.store, core.settings, "worker", core.clock) as wake:
        scan = wake.before_scan()
        assert scan.started_at == datetime.fromisoformat(core.clock())
        assert scan.notification_sequence == wake.notifications
        with pytest.raises(FrozenInstanceError):
            scan.notification_sequence = -1
        core.clock.advance(1)
        wake.before_scan()
        with pytest.raises(ValueError, match="STALE_WORKER_SCAN"):
            run(wake.wait(scan, UnitResult(False)))


@pytest.mark.parametrize("kind", worker_deadlines.PROVIDERS)
def test_domain_deadline_projection_never_performs_recovery_or_accounting(core, kind):
    with core.store.read_snapshot():
        deadline = worker_deadlines.PROVIDERS[kind](
            core.store, core.settings, datetime.fromisoformat(core.clock()),
        )
        assert deadline > datetime.fromisoformat(core.clock())
    assert core.store.rows("SELECT * FROM model_usage") == []


@pytest.mark.parametrize("offset,expected", [(0.1, "deadline"), (-1, "maintenance")])
def test_native_result_deadline_keeps_scan_race_but_ignores_past_fenced_hint(
    core, offset, expected,
):
    with Wakeup(core.store, core.settings, "executive-worker", core.clock) as wake:
        scan = wake.before_scan()
        hint = datetime.fromisoformat(after(core.clock(), offset))
        core.clock.advance(1)

        async def idle():
            return UnitResult(False, next_deadline=hint)

        # A hint crossed after the scan wins; a past hint yields to maintenance.
        result = run(run_native_unit(idle))
        assert run(wake.wait(scan, result, maintenance_at=0)) == expected


@pytest.mark.parametrize("started", [False, True])
def test_native_tool_recovery_distinguishes_leased_from_started_under_shared_runner(core, started):
    fake = FakeExecutor()
    executive = configured(core, fake)
    task = executive.submit(proposal())
    lease = executive.claim()
    if started:
        executive.begin(lease)
    core.clock.advance(100)
    result = run(run_native_unit(executive.run_one))
    assert result.progressed is not started
    assert executive.get(task["task_id"])["state"] == ("UNKNOWN" if started else "SUCCEEDED")
    assert len(fake.calls) == (0 if started else 1)
    assert not run(run_native_unit(executive.run_one)).progressed
    assert len(fake.calls) == (0 if started else 1)


def test_native_unknown_stays_charged_and_is_not_retried_by_shared_runner(core):
    fake = FakeExecutor()
    fake.fail = True
    executive = configured(core, fake)
    task = executive.submit(proposal())
    assert run(run_native_unit(executive.run_one)).progressed
    assert not run(run_native_unit(executive.run_one)).progressed
    row = executive.get(task["task_id"])
    assert row["state"] == "UNKNOWN" and row["billed_micro_usd"] == 0
    assert len(fake.calls) == 1 and row["result_event_id"] is None


@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
def test_native_runner_does_not_swallow_or_retry_cancellation_and_failure(failure):
    calls = []

    async def operation():
        calls.append("started")
        try:
            raise failure()
        finally:
            calls.append("native cleanup")

    with pytest.raises(failure):
        run(run_native_unit(operation))
    assert calls == ["started", "native cleanup"]
