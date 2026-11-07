import asyncio
import json

import httpx

from companion.model import ModelClient
from tests.support.owner_runtime import chat


def reasoning_chat(core, tmp_path, reasoning="合成的私人推理", *, empty=False, tool=False):
    capture, job = chat(core, tmp_path)
    core.store.execute("UPDATE jobs SET state='QUEUED',lease_token=NULL WHERE job_id=%s",
                       (job["job_id"],))
    requests = []

    def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        message = {"content": "" if empty else "synthetic reply"}
        if reasoning is not None:
            message["reasoning_content"] = reasoning + str(len(requests))
        if tool and len(requests) == 1:
            message.update(content=None, tool_calls=[{
                "id": "reasoning-clock", "type": "function", "function": {
                    "name": "current_time", "arguments": "{}"}}])
        return httpx.Response(200, json={"choices": [{"message": message,
            "finish_reason": "length" if empty else "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            core.model = ModelClient(core.settings, client)
            assert await core.run_once("reasoning-worker", job_id=job["job_id"])

    asyncio.run(scenario())
    rows = core.store.rows('SELECT * FROM model_usage WHERE job_id=%s ORDER BY record_sequence',
                           (job["job_id"],))
    assert len(rows) == len(requests)
    return capture, job, rows, requests
