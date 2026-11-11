"""One original chat-worker unit with only the external provider transport mocked."""

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
    states = []

    class Finished(BaseException):
        pass

    def provider(request):
        assert request.url.host == "synthetic.invalid"
        messages = json.loads(request.content)["messages"]
        refs = [json.loads(m["content"].split("\n", 1)[1]) for m in messages
                if isinstance(m.get("content"), str)
                and m["content"].startswith("你的心情参考（数据）：\n")]
        states.append(refs)
        return httpx.Response(200, json=answer("刚才那件事还记着，我们接着聊。"))

    def client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(provider)
        return original_client(**kwargs)

    async def once(self, *args, **kwargs):
        await original_run(self, *args, **kwargs)
        print(json.dumps({"pid": os.getpid(), "calls": states, "jobs": self.store.rows(
            'SELECT state,last_error_code FROM jobs ORDER BY record_sequence')}), flush=True)
        raise Finished

    Core.run_once = once
    core_module.Core = partial(Core, clock=lambda: now)
    entrypoint.httpx = SimpleNamespace(AsyncClient=client)
    try:
        asyncio.run(entrypoint.worker(settings))
    except Finished:
        pass
