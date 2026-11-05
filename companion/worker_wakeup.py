"""Filesystem hints wake native SQLite workers; persisted queues remain authoritative."""

import asyncio
import math
import threading
from datetime import datetime
from pathlib import Path
from time import monotonic

from watchfiles._rust_notify import RustNotify
from watchfiles.main import _default_force_polling, _default_poll_delay_ms

from companion import shutdown
from companion.worker_deadlines import CHAT_WAIT_RESCAN_SECONDS, PROVIDERS
from companion.worker_schedule import ScanContext, UnitResult

# Existing once-per-minute scheduling/retention cadence, measured in elapsed time.
MAINTENANCE_SECONDS = 60


class Maintenance:
    def __init__(self):
        self.next_at = monotonic()

    def due(self):
        return monotonic() >= self.next_at

    def completed(self):
        self.next_at = monotonic() + MAINTENANCE_SECONDS


class Wakeup:
    """Subscribe synchronously, snapshot on the SAME Store, scan, then check before waiting.

    The documented Rust backend provides a subscription barrier and explicit close. Its
    version-pinned defaults retain watchfiles' environment/WSL polling fallback behavior.
    Cancellation joins the outstanding watch call before releasing the native watcher.
    """

    def __init__(self, store, settings, kind, clock):
        if kind not in PROVIDERS:
            raise ValueError("UNKNOWN_WORKER_KIND")
        self.store, self.settings, self.kind, self.clock = store, settings, kind, clock
        self.stop = threading.Event()
        self.watcher = None
        self.scan = None
        self.deadline_provider = PROVIDERS[kind]
        self.notifications = self.version_checks = 0

    def __enter__(self):
        # Watch the parent: SQLite may create/delete/recreate its WAL beside the database.
        self.watcher = RustNotify([str(Path(self.store.path).resolve().parent)], False,
                                  _default_force_polling(None), _default_poll_delay_ms(300),
                                  False, False)
        return self

    def __exit__(self, *_):
        self.stop.set()
        if self.watcher is not None:
            self.watcher.close()

    def data_version(self):
        self.version_checks += 1
        return self.store.one("PRAGMA data_version")["data_version"]

    def before_scan(self) -> ScanContext:
        started_at, started = datetime.fromisoformat(self.clock()), monotonic()
        self.scan = ScanContext(started_at, self.data_version(), started)
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
        # A quiet WAL can miss the notification for the first incoming chat.
        # Poll only the connection's change counter; an idle timeout never claims work.
        interval = (min(CHAT_WAIT_RESCAN_SECONDS, MAINTENANCE_SECONDS)
                    if self.kind == "worker" else MAINTENANCE_SECONDS)
        reconcile_at = started + interval
        while True:
            if shutdown.requested():
                return "shutdown"
            if self.data_version() != scan.data_version:
                return "commit"
            now, mono = datetime.fromisoformat(self.clock()), monotonic()
            if deadline and now >= deadline:
                return "deadline"
            if maintenance_at is not None and mono >= maintenance_at:
                return "maintenance"
            if abs((now - wall).total_seconds() - (mono - started)) > 2:
                return "clock"
            if mono >= reconcile_at:
                reconcile_at = mono + interval
            delay = reconcile_at - mono
            if maintenance_at is not None:
                delay = min(delay, maintenance_at - mono)
            if deadline:
                delay = min(delay, (deadline - now).total_seconds())
            pending = asyncio.create_task(asyncio.to_thread(
                self.watcher.watch, 50, 50, max(1, math.ceil(delay * 1000)),
                shutdown.StopEvent(self.stop),
            ))
            try:
                changes = await asyncio.shield(pending)
            except asyncio.CancelledError:
                self.stop.set()
                await pending
                raise
            if changes == "stop" and not self.stop.is_set() and shutdown.requested():
                return "shutdown"
            if changes in ("stop", "signal"):
                raise asyncio.CancelledError
            if isinstance(changes, set):
                self.notifications += 1
            # Coalesced/self/unrelated filesystem events are only hints. Our own Store
            # commits do not change this connection's data_version; never compare peers.
