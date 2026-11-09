"""Independent file parser and SDK worker; only Docker and model transports are synthetic."""
import asyncio
import json
import pickle
import sys
from pathlib import Path
from types import SimpleNamespace

from companion import file_parser
from companion.retrieval import SemanticIndex
from tests.conftest import Clock
from tests.sdk_reader_fixture import Provider, answer, worker_for
from tests.support.file_parser import FakeProcess

with open(sys.argv[1], "rb") as stream:
    settings, now, job_id, data, output = pickle.load(stream)
clock = Clock()
clock.now = now
SemanticIndex.warmup = lambda self: None
provider = Provider(answer("文件中有 first 和 second。"))
launches = []


def docker(command, **kwargs):
    launches.append(command)
    assert "--network=none" in command and "--pull=never" in command
    result = file_parser.parse_document(data, "notes.txt", "text/plain")
    return FakeProcess(json.dumps(result).encode())


file_parser.subprocess.Popen = docker


async def scenario():
    async with worker_for(SimpleNamespace(settings=settings, clock=clock, store=None),
                          provider) as worker:
        assert await worker.content.run_once()
        assert worker.store.one("SELECT state FROM content_items")["state"] == "SUCCEEDED"
        assert await worker.run_once("independent-file-sdk", job_id=job_id)
        assert worker.store.one('SELECT state FROM jobs WHERE job_id=%s',
                                (job_id,))["state"] == "SUCCEEDED"
        assert len(launches) == 1
        assert not provider.responses
        assert len(worker.store.rows("SELECT * FROM outbox")) == 1


asyncio.run(scenario())
Path(output).write_text(json.dumps(provider.requests, ensure_ascii=False), encoding="utf-8")
