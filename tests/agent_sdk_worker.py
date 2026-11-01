"""Independent synthetic worker for the staged SDK integration candidate."""
import asyncio
import pickle
import sys

import httpx

from companion import core as core_module
from companion import model
from companion.retrieval import SemanticIndex
from tests.conftest import Clock

with open(sys.argv[1], "rb") as stream:
    settings, now, job_id = pickle.load(stream)
clock = Clock()
clock.now = now
SemanticIndex.warmup = lambda self: None
calls = []


async def provider(request):
    calls.append(request)
    return httpx.Response(200, json={"choices": [{"message": {
        "role": "assistant", "content": "independent SDK reply",
        "reasoning_content": "independent synthetic reasoning"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10}})


async def scenario():
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        provider_model = model.ModelClient(settings, client)
        with core_module.Core(settings, provider_model, clock) as core:
            assert await core.run_once("independent-sdk-worker", job_id=job_id)
            row = core.store.one("SELECT state,last_error_code FROM jobs WHERE job_id=?", (job_id,))
            assert row["state"] == "SUCCEEDED", dict(row)
            assert core.store.one("SELECT phase FROM agent_conversations")["phase"] == "COMPLETE"
    assert len(calls) == 1


asyncio.run(scenario())
