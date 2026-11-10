"""Fresh process: real Core SDK, only upstream HTTP mocked and paused before completion."""

import asyncio
import json
import pickle
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx

from tests.conftest import Clock
from tests.sdk_reader_fixture import worker_for
from tests.test_model_stream import chunk

with open(sys.argv[1], 'rb') as source:
    settings, now, job_id, directory = pickle.load(source)
directory = Path(directory)
clock = Clock()
clock.now = now
requests = []


class Gated(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield chunk({'reasoning_content': 'PRIVATE_PROVIDER_REASONING'})
        yield chunk({'content': '真正流式的首段'})
        async with asyncio.timeout(60):
            # A different runtime signals through this private test-only file.
            while not (directory / 'client-saw-preview').exists():  # noqa: ASYNC110
                await asyncio.sleep(0.025)
        yield chunk({'content': '，模型结束后补全。'})
        yield chunk(finish='stop')
        yield chunk(usage={'prompt_tokens': 100, 'completion_tokens': 20})
        yield b'data: [DONE]\n\n'


def provider(request):
    requests.append(json.loads(request.content))
    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Gated())


async def scenario():
    async with worker_for(SimpleNamespace(settings=settings, clock=clock, store=None),
                          provider) as worker:
        assert await worker.run_once('independent-native-stream', job_id=job_id)
        assert worker.store.one('SELECT state,last_error_code FROM jobs WHERE job_id=%s',
                                (job_id,)) == {'state': 'SUCCEEDED', 'last_error_code': None}
        assert len(requests) == 1 and requests[0]['stream'] is True


asyncio.run(scenario())
(directory / 'stream-worker.json').write_text(json.dumps({'requests': len(requests)}))
