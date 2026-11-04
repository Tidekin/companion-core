"""Fresh native chat worker: provider never asks for a memory tool."""

import asyncio
import json
import os
from pathlib import Path

import httpx

from companion.config import Settings
from companion.core import Core
from companion.model import MEMORY_HEADING, ModelClient
from companion.retrieval import SemanticIndex
from tests.sdk_reader_fixture import answer


def run_worker(path, now, job_id, expected):
    settings = Settings.model_validate_json(Path(path).read_text("utf-8"))
    calls = []
    queries = []

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
                model = core.memory.index.warmup()

                class CountedEncoder:
                    passage_embed = model.passage_embed

                    def query_embed(self, query):
                        queries.append(query)
                        return model.query_embed(query)

                counted = CountedEncoder()
                warmup = SemanticIndex.warmup
                SemanticIndex.warmup = lambda _: counted
                try:
                    assert await core.run_once("independent-prefetch", job_id=job_id)
                finally:
                    SemanticIndex.warmup = warmup
                row = core.store.one("SELECT state,last_error_code FROM jobs WHERE job_id=?",
                                     (job_id,))
                assert row["state"] == "SUCCEEDED", row
        assert len(calls) == 1
        print(json.dumps({"pid": os.getpid(), "provider_calls": len(calls),
                          "query_inferences": queries,
                          "payload": calls[0]}, ensure_ascii=False), flush=True)

    asyncio.run(scenario())
