"""Fresh native chat worker: provider never asks for a memory tool."""

import asyncio
import json
import os
from pathlib import Path

import httpx

from companion.config import Settings
from companion.core import Core
from companion.model import MEMORY_HEADING, ModelClient
from tests.sdk_reader_fixture import answer


def run_worker(path, now, job_id, expected):
    settings = Settings.model_validate_json(Path(path).read_text("utf-8"))
    calls = []

    def provider(request):
        assert request.url.host == "synthetic.invalid"
        payload = json.loads(request.content)
        calls.append(payload)
        assert any(message.get("role") == "user" and isinstance(message.get("content"), str)
                   and message["content"].startswith(MEMORY_HEADING)
                   and expected in message["content"] for message in payload["messages"])
        return httpx.Response(200, json=answer("已从相关记录中找到编号。"))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            with Core(settings, ModelClient(settings, client), clock=lambda: now) as core:
                assert await core.run_once("independent-prefetch", job_id=job_id)
                row = core.store.one("SELECT state,last_error_code FROM jobs WHERE job_id=?",
                                     (job_id,))
                assert row["state"] == "SUCCEEDED", row
        assert len(calls) == 1
        print(json.dumps({"pid": os.getpid(), "provider_calls": len(calls),
                          "payload": calls[0]}, ensure_ascii=False), flush=True)

    asyncio.run(scenario())
