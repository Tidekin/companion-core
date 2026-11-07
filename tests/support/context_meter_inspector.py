import json

import httpx
from pydantic_ai.toolsets import FunctionToolset

from companion.agent_runtime import run_conversation
from companion.contracts import CoreError
from companion.model import ModelClient
from tests.owner_model_fixtures import authorized_job
from tests.support.token_measurement import configure


def native_job(core):
    job_id = authorized_job(core)
    return core.store.one((
        'SELECT j.*,e.conversation_id,e.body_json,e.trace_id FROM jobs j JOIN'
        ' events e USING(event_id) WHERE job_id=%s'
    ), (job_id,))


async def recorded_attempts(core, tmp_path, *, unknown=False):
    configure(core, tmp_path)
    job = native_job(core)
    requests = []

    def first() -> str:
        """Read a synthetic observation."""
        return "SYNTHETIC_PRIVATE_TOOL_RESULT" * 10

    def second() -> str:
        """Read a different synthetic observation."""
        return "SYNTHETIC_SECOND_RESULT"

    def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        result = {"choices": [{"message": {"content": "synthetic reply"}}]}
        if len(requests) == 1:
            result["choices"][0]["message"] = {"content": None, "tool_calls": [
                {"id": name + "-call", "type": "function", "function": {
                    "name": name, "arguments": "{}"}} for name in ("first", "second")]}
        if not unknown or len(requests) == 1:
            result["usage"] = {"prompt_tokens": 100, "completion_tokens": 4}
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        core.model = ModelClient(core.settings, client)
        try:
            await run_conversation(core, job, core.context(job),
                                   toolsets=[FunctionToolset([first, second])])
        except CoreError as error:
            assert unknown and error.code == "MODEL_OUTCOME_UNKNOWN"
        else:
            assert not unknown
    return job, requests
