"""One real Core step in a fresh process with only provider HTTP mocked."""

import asyncio
import json
import pickle
import sys
from pathlib import Path

from tests.conftest import Clock
from tests.sdk_reader_fixture import Provider, worker_for

with open(sys.argv[1], "rb") as stream:
    settings, now, job_id, responses, expected, output = pickle.load(stream)
clock = Clock()
clock.now = now
provider = Provider(*responses)


async def scenario():
    from types import SimpleNamespace

    async with worker_for(SimpleNamespace(settings=settings, clock=clock, store=None),
                          provider) as worker:
        assert await worker.run_once("independent-reader-sdk", job_id=job_id)
        row = worker.store.one("SELECT state,last_error_code FROM jobs WHERE job_id=?", (job_id,))
        assert row["state"] == expected, row
        assert not provider.responses


asyncio.run(scenario())
Path(output).write_text(json.dumps(provider.requests, ensure_ascii=False), encoding="utf-8")
