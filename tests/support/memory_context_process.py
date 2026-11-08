"""One native CLI worker with synthetic provider transport and restore fault injection."""

import asyncio
import json
import os
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx

from companion import __main__ as entrypoint
from companion import core as core_module
from companion.agent_persistence import ConversationStore
from companion.config import Settings
from companion.core import Core
from tests.sdk_reader_fixture import answer, call


def run_worker(settings_path, now, mode="normal"):
    settings = Settings.model_validate_json(Path(settings_path).read_text("utf-8"))
    assert settings.model_url == "https://synthetic.invalid/v1"
    original_client, original_run = httpx.AsyncClient, Core.run_once
    original_tool = ConversationStore.wrap_tool_execute
    calls = []

    class Finished(BaseException):
        pass

    def provider(request):
        assert request.url.host == "synthetic.invalid"
        payload = json.loads(request.content)
        calls.append(payload)
        exam = "台布前后" in str(payload["messages"])
        has_tool = any(message["role"] == "tool" for message in payload["messages"])
        if exam and not has_tool:
            result = answer(calls=[call("recall-colors", "memory_recall", {"query": "台布颜色"})])
        else:
            result = answer("前面说靛青，后来又说砂黄，两个说法都保留。" if exam else "记住了。")
        return httpx.Response(200, json=result)

    def client(**kwargs):
        return original_client(**{**kwargs, "transport": httpx.MockTransport(provider)})

    async def tool(self, *args, **kwargs):
        result = await original_tool(self, *args, **kwargs)
        if mode == "pause_after_tool":
            raise Finished
        return result

    def blocked_load(self):
        self.store.one("SELECT pg_sleep(2)")
        raise AssertionError("restore SQL deadline was not enforced")

    async def once(self, *args, **kwargs):
        try:
            await original_run(self, *args, **kwargs)
        finally:
            print(json.dumps({"pid": os.getpid(), "calls": calls, "jobs": self.store.rows(
                "SELECT job_id,state,last_error_code,attempts FROM jobs ORDER BY record_sequence"),
                "usage": self.store.rows("SELECT request_id,state FROM model_usage "
                                         "ORDER BY record_sequence")}), flush=True)
        raise Finished

    if mode == "busy_restore":
        ConversationStore.load = blocked_load
    ConversationStore.wrap_tool_execute = tool
    Core.run_once = once
    core_module.Core = partial(Core, clock=lambda: now)
    entrypoint.httpx = SimpleNamespace(AsyncClient=client)
    try:
        asyncio.run(entrypoint.worker(settings))
    except Finished:
        pass
