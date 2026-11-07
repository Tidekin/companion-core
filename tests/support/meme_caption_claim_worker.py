"""Actual worker CLI with a pre-caption-claim barrier and mocked provider transport."""

import asyncio
import json
import os
import sys
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx

from companion import __main__ as entrypoint
from companion import core as core_module
from companion import shutdown
from companion.config import Settings
from companion.core import Core
from companion.meme_core import MemeCaptionService
from companion.worker_schedule import ClaimBusy
from tests.support.meme_core import CAPTION


def main():
    settings_path, now, outcome = sys.argv[1:]
    assert outcome in {"resume", "stop"}
    settings = Settings.model_validate_json(Path(settings_path).read_text("utf-8"))
    native, client, completed = (MemeCaptionService.run_once, httpx.AsyncClient,
                                 entrypoint.Recorder.completed)
    calls, busy = [], []

    async def caption(service, worker_id):
        service.store.db.execute("PRAGMA busy_timeout=1")
        if not getattr(caption, "ready", False):
            caption.ready = True
            print(json.dumps({"phase": "ready", "pid": os.getpid()}), flush=True)
            assert (await asyncio.to_thread(sys.stdin.readline)).strip() == "continue"
        try:
            return await native(service, worker_id)
        except ClaimBusy:
            busy.append(1)
            print(json.dumps({"phase": "busy", "provider_calls": len(calls),
                              "in_transaction": service.store.db.in_transaction}), flush=True)
            if outcome == "stop":
                shutdown.request()
            raise  # Only the actual worker loop decides whether to scan again.

    def provider(request):
        assert request.url.host == "synthetic.invalid"
        calls.append(1)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(CAPTION)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        })

    def offline_client(**kwargs):
        return client(transport=httpx.MockTransport(provider), **kwargs)

    def stop(recorder, **kwargs):
        completed(recorder, **kwargs)
        if not kwargs["maintenance"] and not kwargs["waiting"]:
            shutdown.request()

    MemeCaptionService.run_once = caption
    core_module.Core = partial(Core, clock=lambda: now)
    entrypoint.Settings.load = classmethod(lambda cls: settings)
    entrypoint.Recorder.completed = stop
    entrypoint.httpx = SimpleNamespace(AsyncClient=offline_client)
    sys.argv = ["companion", "worker"]
    entrypoint.main()
    print(json.dumps({"phase": "finished", "pid": os.getpid(),
                      "provider_calls": len(calls), "busy_claims": len(busy)}), flush=True)


if __name__ == "__main__":
    main()
