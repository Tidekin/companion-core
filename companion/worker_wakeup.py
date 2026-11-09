"""Native PostgreSQL wake hints; persisted queues and periodic scans own correctness."""

import asyncio
from datetime import datetime
from time import monotonic

from companion import shutdown
from companion.worker_deadlines import PROVIDERS
from companion.worker_schedule import ScanContext, UnitResult

MAINTENANCE_SECONDS = 60
# Bounds cancellation and SIGTERM even when the listener receives no hints.
LISTENER_WAIT_SECONDS = 0.25


class Maintenance:
    def __init__(self):
        self.next_at = monotonic()

    def due(self):
        return monotonic() >= self.next_at

    def completed(self):
        self.next_at = monotonic() + MAINTENANCE_SECONDS


class Wakeup:
    """LISTEN completes before scanning; a missed hint always gets a timed rescan.

    The dedicated native connection never takes a Store/request pool slot. One
    finite notifies() call runs off-loop; cancellation joins it before closing.
    https://www.psycopg.org/psycopg3/docs/advanced/async.html#asynchronous-notifications
    """

    def __init__(self, store, settings, kind, clock):
        if kind not in PROVIDERS:
            raise ValueError("UNKNOWN_WORKER_KIND")
        self.store, self.settings, self.kind, self.clock = store, settings, kind, clock
        self.listener = self._context = self.scan = None
        self.deadline_provider = PROVIDERS[kind]
        self.notifications = self.notification_checks = 0

    def __enter__(self):
        self._context = self.store.database.listener()
        self.listener = self._context.__enter__()
        return self

    def __exit__(self, *args):
        self._context.__exit__(*args)

    def _receive(self, timeout):
        self.notification_checks += 1
        return list(self.listener.notifies(timeout=timeout, stop_after=128))

    def _accept(self, values):
        self.notifications += sum(value.pid != self.store.backend_pid for value in values)

    def before_scan(self) -> ScanContext:
        self._accept(self._receive(0))
        self.scan = ScanContext(
            datetime.fromisoformat(self.clock()), self.notifications, monotonic())
        return self.scan

    async def wait(self, scan: ScanContext, result: UnitResult, maintenance_at=None):
        if scan is not self.scan:
            raise ValueError("STALE_WORKER_SCAN")
        if result.progressed:
            return "progress"
        wall = datetime.fromisoformat(self.clock())
        deadline = self.deadline_provider(self.store, self.settings, scan.started_at)
        if result.next_deadline is not None and result.next_deadline >= scan.started_at:
            deadline = min(deadline, result.next_deadline)
        started = monotonic()
        # LISTEN and persisted job deadlines wake earlier; an idle queue only
        # needs a full rescan at the regular maintenance boundary.
        reconcile_at = started + MAINTENANCE_SECONDS
        while True:
            if shutdown.requested():
                return "shutdown"
            if self.notifications != scan.notification_sequence:
                return "commit"
            now, mono = datetime.fromisoformat(self.clock()), monotonic()
            if deadline and now >= deadline:
                return "deadline"
            if maintenance_at is not None and mono >= maintenance_at:
                return "maintenance"
            if abs((now - wall).total_seconds() - (mono - started)) > 2:
                return "clock"
            if mono >= reconcile_at:
                return "reconcile"
            delay = min(LISTENER_WAIT_SECONDS, reconcile_at - mono)
            if maintenance_at is not None:
                delay = min(delay, maintenance_at - mono)
            if deadline:
                delay = min(delay, (deadline - now).total_seconds())
            pending = asyncio.create_task(asyncio.to_thread(self._receive, max(0, delay)))
            try:
                values = await asyncio.shield(pending)
            except asyncio.CancelledError:
                await pending
                raise
            self._accept(values)
