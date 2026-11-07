import json

import httpx

from companion.config import ObservationAdapterConfig
from tests.sdk_reader_fixture import answer, worker_for

OBSERVATION = {"observation_adapters": {"content_image": ObservationAdapterConfig(
    source="qq_image", token="synthetic-image-reference-token-12345", media_analysis=True)}}


async def reply(core, entry, text, calls):
    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=answer(text))

    async with worker_for(core, provider) as worker:
        assert await worker.run_once()
    delivered = await entry.send(entry.client, entry.protocol)
    assert delivered["attempt_states"].get("ACK")
