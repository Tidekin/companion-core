"""Observe the real long-lived worker and wait loop; no external service is invoked."""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic

import httpx

from companion import __main__, worker_wakeup
from companion.config import Settings
from companion.core import Core
from companion.retrieval import SemanticIndex


def main():
    args = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    settings = Settings.model_validate(args["settings"])
    process_start, start = monotonic(), None
    anchor = datetime.fromisoformat(args["now"])
    report = {"pid": os.getpid(), "scans": [], "waits": [], "maintenance": [],
              "provider_calls": 0, "expiry_queries": 0, "watchers": 0, "closed_watchers": 0,
              "production_maintenance_seconds": worker_wakeup.MAINTENANCE_SECONDS,
              "test_maintenance_seconds": args["maintenance_seconds"], "complete": False,
              "clock_origin": "first_native_scan"}
    worker_wakeup.MAINTENANCE_SECONDS = args["maintenance_seconds"]
    SemanticIndex.warmup = lambda self: None
    native_init, native_scan = Core.__init__, Core.run_once

    def clock():
        # Initialization/subscription cannot consume the future deadline fixture.
        elapsed = 0 if start is None else monotonic() - start
        return (anchor + timedelta(seconds=elapsed)).isoformat()

    def initialize(self, config, model, *a, **kw):
        native_init(self, config, model, clock=clock)
        fetch = self.store.rows

        def observed(statement, params=()):
            if isinstance(statement, str) and "WHERE voice_jobs.content_retired = 0" in statement:
                report["expiry_queries"] += 1
                assert "LIMIT 1000" in statement
            return fetch(statement, params)

        self.store.rows = observed

    async def scan(self, *a, **kw):
        nonlocal start
        if start is None:
            start = monotonic()
            report["startup_seconds"] = start - process_start
        before = self.store.one("SELECT COUNT(*) n FROM voice_jobs WHERE content_retired=0")["n"]
        began = monotonic() - start
        worked = await native_scan(self, *a, **kw)
        remaining = self.store.one("SELECT COUNT(*) n FROM voice_jobs WHERE content_retired=0")["n"]
        report["scans"].append({"started": began, "ended": monotonic() - start,
                                "before": before, "remaining": remaining, "worked": worked})
        if not remaining:
            report["complete"] = True
            asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
        return worked

    Core.__init__, Core.run_once = initialize, scan
    native_completed = worker_wakeup.Maintenance.completed

    def completed(self):
        native_completed(self)
        report["maintenance"].append({"at": monotonic() - start,
                                      "next_at": self.next_at - start})

    worker_wakeup.Maintenance.completed = completed
    native_enter, native_exit = worker_wakeup.Wakeup.__enter__, worker_wakeup.Wakeup.__exit__
    native_wait = worker_wakeup.Wakeup.wait

    def enter(self):
        result = native_enter(self)
        report["watchers"] += 1
        return result

    def close(self, *a):
        result = native_exit(self, *a)
        report["closed_watchers"] += 1
        return result

    async def wait(self, *a):
        row = {"entered": monotonic() - start, "reason": "cancelled"}
        report["waits"].append(row)
        try:
            row["reason"] = await native_wait(self, *a)
            return row["reason"]
        finally:
            row["returned"] = monotonic() - start
            row["notifications"] = self.notifications

    worker_wakeup.Wakeup.__enter__, worker_wakeup.Wakeup.__exit__ = enter, close
    worker_wakeup.Wakeup.wait = wait
    native_client = httpx.AsyncClient

    def provider(_request):
        report["provider_calls"] += 1
        return httpx.Response(503, json={"error": "unexpected synthetic external call"})

    class SyntheticClient(native_client):
        def __init__(self, **kw):
            super().__init__(**(kw | {"transport": httpx.MockTransport(provider)}))

    httpx.AsyncClient = SyntheticClient
    native_worker = __main__.worker

    async def bounded(config):
        task = asyncio.current_task()
        stop = asyncio.get_running_loop().call_later(30, task.cancel)
        try:
            await native_worker(config)
        except asyncio.CancelledError:
            pass
        finally:
            stop.cancel()

    __main__.worker = bounded
    Settings.load = classmethod(lambda cls: settings)
    sys.argv = ["companion", "worker"]
    __main__.main()
    report["elapsed"] = monotonic() - start
    Path(args["report"]).write_text(json.dumps(report), encoding="utf-8")


if __name__ == "__main__":
    main()
