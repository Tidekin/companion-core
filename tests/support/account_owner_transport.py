import asyncio
import json

import httpx

from companion.model import ModelClient
from tests.support.social_autonomy import background, queue
from tests.support.social_owner_authority import authorize


def prepared(core, tmp_path):
    module, source, conversation = background(core, max_output_tokens=512)
    _, ref = authorize(core, tmp_path)

    async def plan():
        def reply(request):
            assert request.url.host == "synthetic.invalid"
            data = json.loads(json.loads(request.content)["messages"][-1]["content"])
            proposal = {"kind": "qzone_publish", "text": "Synthetic public observation",
                        "source_refs": [data["sources"][0]["ref"]]}
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps(proposal)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply),
                                     trust_env=False) as client:
            module.model = ModelClient(core.settings, client)
            return await module.plan("account-transport-test", source_ids=[source])

    frozen = queue(module, asyncio.run(plan()), conversation)
    return ref, frozen
