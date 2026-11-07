"""Native queue hints, rollback, missed-notification rescans and cancellation."""

import asyncio
from contextlib import closing
from time import monotonic

import pytest

from companion import worker_deadlines, worker_wakeup
from companion.store import Store, utcnow
from companion.worker_schedule import UnitResult
from companion.worker_wakeup import Wakeup
from tests.conftest import run


@pytest.mark.parametrize("kind", list(worker_deadlines.PROVIDERS))
def test_committed_peer_wakes_each_native_worker(core, kind):
    async def scenario():
        with Wakeup(core.store, core.settings, kind, utcnow) as wake:
            scan = wake.before_scan()
            with closing(Store(core.store.database, core.settings.state_directory)) as other:
                with other.transaction():
                    other.execute("INSERT INTO runtime_meta(key,value) VALUES ('peer','1')")
            assert await asyncio.wait_for(wake.wait(scan, UnitResult(False)), 2) == "commit"
            assert core.store.one("SELECT value FROM runtime_meta WHERE key='peer'") == {
                "value": "1"}
    run(scenario())


def test_rollback_and_self_hints_do_not_wake_another_business_scan(core):
    async def scenario():
        with Wakeup(core.store, core.settings, "worker", utcnow) as wake:
            scan = wake.before_scan()
            with closing(Store(core.store.database, core.settings.state_directory)) as other:
                with pytest.raises(ValueError), other.transaction():
                    other.execute("INSERT INTO runtime_meta(key,value) VALUES ('rolled-back','1')")
                    raise ValueError("synthetic transaction rollback")
            with core.store.transaction():
                core.store.execute("INSERT INTO runtime_meta(key,value) VALUES ('self','1')")
            started = monotonic()
            assert await wake.wait(scan, UnitResult(False), started + .15) == "maintenance"
            assert monotonic() - started >= .14
            assert core.store.one("SELECT 1 FROM runtime_meta WHERE key='rolled-back'") is None
    run(scenario())


def test_missing_all_notifications_still_returns_for_a_real_rescan(core, monkeypatch):
    monkeypatch.setattr(worker_wakeup, "CHAT_WAIT_RESCAN_SECONDS", .08)

    async def scenario():
        with Wakeup(core.store, core.settings, "worker", utcnow) as wake:
            scan = wake.before_scan()
            # Direct native writers need not publish a hint for correctness.
            with closing(Store(core.store.database, core.settings.state_directory)) as other:
                other.execute("INSERT INTO runtime_meta(key,value) VALUES ('missed','1')")
            started = monotonic()
            assert await asyncio.wait_for(wake.wait(scan, UnitResult(False)), 1) == "reconcile"
            assert monotonic() - started >= .07
            assert core.store.one("SELECT value FROM runtime_meta WHERE key='missed'") == {
                "value": "1"}
    run(scenario())


def test_cancel_joins_finite_listener_call_before_closing(core):
    async def scenario():
        with Wakeup(core.store, core.settings, "worker", utcnow) as wake:
            listener = wake.listener
            pending = asyncio.create_task(wake.wait(wake.before_scan(), UnitResult(False)))
            await asyncio.sleep(.02)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 1)
            assert not listener.closed
        assert listener.closed
    run(scenario())
