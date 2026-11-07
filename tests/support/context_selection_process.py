"""One original worker iteration; only provider transport and the clock are replaced."""

import asyncio
import json
import os
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx

from companion import __main__ as entrypoint
from companion import core as core_module
from companion.config import Settings
from companion.core import Core
from tests.sdk_reader_fixture import answer


def run_worker(settings_path, now):
    settings = Settings.model_validate_json(Path(settings_path).read_text("utf-8"))
    assert settings.model_url == "https://synthetic.invalid/v1"
    original_client, original_run = httpx.AsyncClient, Core.run_once
    calls = []

    class Finished(BaseException):
        pass

    def provider(request):
        assert request.url.host == "synthetic.invalid"
        payload = json.loads(request.content)
        calls.append(payload)
        try:
            data = json.loads(payload["messages"][-1]["content"])
        except (ValueError, TypeError):
            data = {}
        if data.get("operation") == "recall":
            text = json.dumps({"relevant": [row["ref"] for row in data["memories"]]})
        else:
            text = "星光资料的旧判断仍有依据，光谱标注的疑问也记着。"
        return httpx.Response(200, json=answer(text))

    def client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(provider)
        return original_client(**kwargs)

    async def once(self, *args, **kwargs):
        await original_run(self, *args, **kwargs)
        print(json.dumps({"pid": os.getpid(), "calls": calls, "jobs": self.store.rows(
            'SELECT job_id,state,last_error_code FROM jobs ORDER BY record_sequence'),
            "usage": self.store.rows(
                'SELECT scenario,state FROM model_usage ORDER BY '
                'record_sequence'
            )}),
            flush=True)
        raise Finished

    Core.run_once = once
    core_module.Core = partial(Core, clock=lambda: now)
    entrypoint.httpx = SimpleNamespace(AsyncClient=client)
    try:
        asyncio.run(entrypoint.worker(settings))
    except Finished:
        pass
