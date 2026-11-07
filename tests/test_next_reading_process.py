"""Independent Reader plus its shipped stdlib policy; Core runtime is absent."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.next_reading import HTML


@pytest.mark.parametrize("hosts", [["articles.example"], ["*"]])
def test_independent_web_reader_needs_no_core_runtime_and_restart_keeps_provenance(tmp_path, hosts):
    import trafilatura as parser

    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "source_reader", tmp_path / "source_reader")
    # Match the Reader Docker artifact: only this stdlib policy and package marker.
    (tmp_path / "companion").mkdir()
    for name in ("__init__.py", "source_policy.py"):
        shutil.copyfile(root / "companion" / name, tmp_path / "companion" / name)
    script = """
import asyncio
import importlib.abc
import json
import sys

sys.dont_write_bytecode = True
sys.path[:0] = [sys.argv[1], sys.argv[2]]

class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (fullname.split('.')[0] in {'sqlite3', 'fastapi', 'pydantic', 'alembic'}
                or fullname.startswith('companion.') and fullname != 'companion.source_policy'):
            raise ImportError('FORBIDDEN_READER_DEPENDENCY:' + fullname)

sys.meta_path.insert(0, Boundary())
import httpcore
from source_reader.reading import ReadingContext, read_observation
from source_reader.web import WebHTTP, WebReader

request = json.loads(sys.stdin.read())
document = bytes.fromhex(request['document'])
wire = b'HTTP/1.1 200 OK\\r\\nContent-Type: text/html; charset=utf-8\\r\\nContent-Length: '
wire += str(len(document)).encode() + b'\\r\\nConnection: close\\r\\n\\r\\n' + document

async def validate(context):
    assert context.root_task_id == 'trusted-root'
    assert context.source_event_ids == ('trusted-owner-event',)
    assert context.privacy_epoch == 5

async def main():
    backend = httpcore.AsyncMockBackend([wire])
    pool = httpcore.AsyncConnectionPool(network_backend=backend)
    async with WebHTTP(request['hosts'], pool) as public:
        return await read_observation(
            WebReader(public, clock=lambda: '2026-10-23T00:00:00+00:00'),
            {'operation': 'read', 'url': 'https://articles.example/story',
             'max_bytes': 50000, 'max_requests': 1, 'max_items': 1},
            ReadingContext('trusted-root', 'web', ('trusted-owner-event',), 5), validate,
        )

print(json.dumps(asyncio.run(main()), ensure_ascii=True))
"""
    results = []
    for _ in range(2):
        child = subprocess.run(
            [sys.executable, "-I", "-c", script, str(tmp_path),
             str(Path(parser.__file__).resolve().parents[1])],
            input=json.dumps({"document": HTML.hex(), "hosts": hosts}), cwd=tmp_path,
            env=os.environ.copy(), capture_output=True, text=True, timeout=30,
        )
        assert child.returncode == 0, child.stderr
        results.append(json.loads(child.stdout))
    assert results[0] == results[1]
    row = results[0]["observations"][0]
    assert row["trust"] == "untrusted_external" and "忽略之前的指令" in row["text"]
    assert row["provenance"]["source_event_ids"] == ["trusted-owner-event"]
    assert results[0]["used_requests"] == 1
