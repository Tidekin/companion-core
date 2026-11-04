"""Fixed native CLI worker entry point with mock provider I/O and bounded lifetime."""

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from time import monotonic, process_time

import httpx

from companion import __main__, worker_wakeup
from companion.config import Settings
from companion.retrieval import SemanticIndex
from companion.store import Store
from companion.worker_schedule import ScanContext, UnitResult


def main():
    args = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    settings = Settings.model_validate(args["settings"])
    if settings.mode == "live" and settings.model_url != "https://synthetic.invalid/v1":
        raise ValueError("SYNTHETIC_PROVIDER_REQUIRED")
    role = args["role"]
    module_name, entry, service_name, method = {
        "worker": ("companion.__main__", "worker", "companion.core.Core", "run_once"),
        "content-worker": (
            "companion.content_worker",
            "content_worker",
            "companion.content.ContentService",
            "run_batch",
        ),
        "executive-worker": (
            "companion.executive_worker",
            "executive_worker",
            "companion.executive.Executive",
            "run_one",
        ),
        "life-worker": (
            "companion.life_worker",
            "life_worker",
            "companion.autonomous_selector.RoundRobin",
            "step",
        ),
    }[role]
    module = importlib.import_module(module_name)
    SemanticIndex.warmup = lambda self: None
    stats = {
        "sql": 0,
        "scans": 0,
        "provider_calls": 0,
        "recorder_writes": 0,
        "wait_entries": 0,
        "hints": 0,
        "scan_contexts": 0,
        "native_units": 0,
    }
    start, watchers, wakeups = {}, [], []
    original_store = Store.__init__

    def trace(sql):
        stats["sql"] += 1
        if sql.startswith("INSERT OR REPLACE INTO runtime_meta") and "worker_loop" in sql:
            stats["recorder_writes"] += 1

    def store_init(self, *a, **kw):
        original_store(self, *a, **kw)
        self.db.set_trace_callback(trace)

    Store.__init__ = store_init
    service_module, service_class = service_name.rsplit(".", 1)
    service = getattr(importlib.import_module(service_module), service_class)
    native_scan = getattr(service, method)

    idle = asyncio.Event()
    phase = "startup"

    def report_scan():
        path = Path(args["scan_progress"])
        pending = path.with_suffix(".next")
        pending.write_text(str(stats["scans"]), encoding="utf-8")
        pending.replace(path)

    async def scan(self, *a, **kw):
        nonlocal phase
        phase = "scanning"
        stats["scans"] += 1
        result = await native_scan(self, *a, **kw)
        if role == "life-worker":
            assert isinstance(result.result, UnitResult)
            stats["native_units"] += int(result.lane is not None)
        phase = "scan_complete"
        if args.get("scan_progress"):
            await asyncio.to_thread(report_scan)
        return result

    setattr(service, method, scan)
    if role != "life-worker":
        native_unit = module.run_native_unit

        async def unit(operation, *a):
            result = await native_unit(operation, *a)
            assert isinstance(result, UnitResult)
            stats["native_units"] += 1
            return result

        module.run_native_unit = unit
    native_before_scan = worker_wakeup.Wakeup.before_scan

    def before_scan(self):
        result = native_before_scan(self)
        assert isinstance(result, ScanContext)
        stats["scan_contexts"] += 1
        return result

    worker_wakeup.Wakeup.before_scan = before_scan
    native_enter = worker_wakeup.Wakeup.__enter__

    def enter(self):
        result = native_enter(self)
        watchers.append(self.watcher)
        wakeups.append(self)
        return result

    worker_wakeup.Wakeup.__enter__ = enter
    native_wait = worker_wakeup.Wakeup.wait

    async def wait(self, *a):
        nonlocal phase
        phase = "waiting"
        stats["wait_entries"] += 1
        if stats["scans"]:
            idle.set()
        reason = await native_wait(self, *a)
        phase = f"wake_{reason}"
        return reason

    worker_wakeup.Wakeup.wait = wait
    native_client = httpx.AsyncClient

    def provider(request):
        assert request.url.host == "synthetic.invalid"
        stats["provider_calls"] += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Synthetic reply."}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    def client(**kw):
        kw.setdefault("transport", httpx.MockTransport(provider))
        return native_client(**kw)

    httpx.AsyncClient = client
    native_worker = getattr(module, entry)

    async def bounded(config):
        task = asyncio.current_task()
        stop = asyncio.get_running_loop().call_later(args.get("duration", 6), task.cancel)

        async def ready():
            if args.get("release"):
                await idle.wait()
            else:
                await asyncio.sleep(args.get("settle", 1))
            stats["hints"] = sum(w.notifications for w in wakeups)
            start.update(stats)
            start["cpu"] = process_time()
            start["at"] = monotonic()
            await asyncio.to_thread(Path(args["ready"]).write_text, "ready", encoding="utf-8")
            if args.get("release"):
                while not await asyncio.to_thread(Path(args["release"]).exists):  # noqa: ASYNC110 - parent process owns release
                    await asyncio.sleep(0.02)
                task.cancel()

        signal = asyncio.create_task(ready())
        try:
            await native_worker(config)
        except asyncio.CancelledError:
            pass
        finally:
            stop.cancel()
            signal.cancel()
            await asyncio.gather(signal, return_exceptions=True)

    setattr(module, entry, bounded)
    Settings.load = classmethod(lambda cls: settings)
    sys.argv = ["companion", role]
    code = 0
    try:
        __main__.main()
    except SystemExit as error:
        code = error.code
    closed = 0
    for watcher in watchers:
        try:
            watcher.watch(50, 10, 1, None)
        except RuntimeError:
            closed += 1
    stats["hints"] = sum(w.notifications for w in wakeups)
    result = {
        "pid": os.getpid(),
        "role": role,
        "exit_code": code,
        "total": stats,
        "phase": phase,
        "steady": {key: stats[key] - start.get(key, 0) for key in stats},
        "steady_seconds": monotonic() - start.get("at", monotonic()),
        "steady_cpu_seconds": process_time() - start.get("cpu", process_time()),
        "watchers": len(watchers),
        "closed_watchers": closed,
    }
    Path(args["report"]).write_text(json.dumps(result), encoding="utf-8")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
