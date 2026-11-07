import json

import httpx

from companion import budget
from companion.contracts import CoreError
from companion.model import ModelClient
from tests.conftest import run
from tests.owner_model_fixtures import authorized_job

MESSAGES = [{"role": "user", "content": "synthetic cache observation"}]


ABSENT = object()


def attempt(core, *, cache=ABSENT, usage=ABSENT, status=200, number=1, marker=ABSENT):
    request_id = budget.reserve(core.store, core.settings, authorized_job(core, number=number),
                                "chat", MESSAGES, core.clock())
    body = {"choices": [{"message": {"content": "synthetic reply"}}]}
    if usage is ABSENT:
        usage = {"prompt_tokens": 80, "completion_tokens": 3}
        if cache is not ABSENT:
            usage["prompt_cache_hit_tokens"] = cache
    if usage != "NO_USAGE":
        body["usage"] = usage
    calls = []

    class Client(ModelClient):
        async def complete(self, *args, **kwargs):
            result = await super().complete(*args, **kwargs)
            if marker is not ABSENT:
                result.cache_usage_present = marker
            return result

    async def exercise():
        def provider(request):
            calls.append(json.loads(request.content))
            return httpx.Response(status, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            try:
                return await budget.complete(Client(core.settings, client), core.store,
                    core.settings, request_id, MESSAGES, "chat", core.clock)
            except CoreError as error:
                return error

    response = run(exercise())
    assert len(calls) == 1
    row = core.store.one("SELECT * FROM model_usage WHERE request_id=?", (request_id,))
    record = core.store.one("SELECT * FROM audit_log WHERE action='model.usage_diagnostic' "
                            "AND entity_id=?", (request_id,))
    return response, row, record
