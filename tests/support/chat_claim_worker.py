"""Run the actual chat loop with a controlled pre-claim writer and mock model I/O."""

import asyncio
import json
import os
import sqlite3
import sys
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx

from companion import __main__ as entrypoint
from companion import core as core_module
from companion import runtime_config, shutdown
from companion.config import Settings
from companion.core import Core
from tests.sdk_reader_fixture import answer


def run_worker(settings_path, now):
    settings = Settings.model_validate_json(Path(settings_path).read_text("utf-8"))
    calls, busy = [], []
    client, boundary, completed = (
        httpx.AsyncClient, runtime_config.worker_boundary, entrypoint.Recorder.completed,
    )

    class Worker(Core):
        def claim_job(self, *args, **kwargs):
            self.store.db.execute("PRAGMA busy_timeout=1")
            try:
                return super().claim_job(*args, **kwargs)
            except sqlite3.OperationalError as error:
                if getattr(error, "sqlite_errorcode", 0) & 0xFF == sqlite3.SQLITE_BUSY:
                    busy.append(1)
                    print(json.dumps({"phase": "busy"}), flush=True)
                raise

    async def ready(*args):
        active = await boundary(*args)
        if active and not getattr(ready, "signalled", False):
            ready.signalled = True
            print(json.dumps({"phase": "ready", "pid": os.getpid()}), flush=True)
            assert (await asyncio.to_thread(sys.stdin.readline)).strip() == "continue"
        return active

    def provider(request):
        assert request.url.host == "synthetic.invalid"
        calls.append(1)
        return httpx.Response(200, json=answer("第一段合成回答。\n第二段合成回答。"))

    def offline_client(**kwargs):
        return client(transport=httpx.MockTransport(provider), **kwargs)

    def stop(recorder, **kwargs):
        completed(recorder, **kwargs)
        if kwargs["maintenance"] is False and not kwargs["waiting"]:
            shutdown.request()

    core_module.Core = partial(Worker, clock=lambda: now)
    runtime_config.worker_boundary = ready
    entrypoint.Recorder.completed = stop
    entrypoint.httpx = SimpleNamespace(AsyncClient=offline_client)
    asyncio.run(entrypoint.worker(settings))
    print(json.dumps({"phase": "finished", "pid": os.getpid(),
                      "provider_calls": len(calls), "busy_claims": len(busy)}), flush=True)
