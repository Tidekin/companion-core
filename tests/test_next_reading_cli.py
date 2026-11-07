"""Actual CLI in another process, real local HTTP to a mock Core, fake public bytes."""

import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from companion.reading_protocol import ReadObservation
from tests.support.next_reading import HTML
from tests.support.next_reading_worker import TOKEN, lease


@pytest.mark.parametrize("protocol", ["legacy", "reading-v1"])
def test_cli_uses_real_http_from_independent_process_and_never_imports_core(protocol):
    received = []
    item = lease(lease_until=(datetime.now(UTC) + timedelta(seconds=30)).isoformat())

    class CoreHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw or b"{}")
            received.append((self.path, self.headers.get("Authorization"), body))
            if self.path.endswith("/lease"):
                payload = {"item": item if protocol == "reading-v1" else None}
            elif self.path.endswith("/validate"):
                payload = {"valid": True}
            elif self.path.endswith("/result"):
                payload = {"state": "DONE", "duplicate": False}
            else:
                self.send_error(404)
                return
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), CoreHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    script = """
import importlib.abc
import json
import runpy
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])

class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fastapi', 'pydantic', 'alembic', 'sqlite3'}:
            raise ImportError('FORBIDDEN_READER_DEPENDENCY:' + fullname)
        if fullname.startswith('companion.') and fullname != 'companion.source_policy':
            raise ImportError('FORBIDDEN_CORE_MODULE:' + fullname)

sys.meta_path.insert(0, Boundary())
import httpcore
from source_reader import worker_v1
from source_reader.web import WebHTTP

document = bytes.fromhex(json.loads(sys.stdin.read())['document'])
wire = b'HTTP/1.1 200 OK\\r\\nContent-Type: text/html; charset=utf-8\\r\\nContent-Length: '
wire += str(len(document)).encode() + b'\\r\\nConnection: close\\r\\n\\r\\n' + document

def provider(hosts):
    assert hosts == {'articles.example'}
    pool = httpcore.AsyncConnectionPool(network_backend=httpcore.AsyncMockBackend([wire]))
    return WebHTTP(hosts, pool)

worker_v1.WebHTTP = provider
origin, protocol = sys.argv[2:4]
sys.argv = ['source_reader', '--core-url', origin, '--adapter-id', 'fixture', '--once']
if protocol == 'reading-v1':
    sys.argv += ['--source', 'web', '--protocol', 'reading-v1']
runpy.run_module('source_reader', run_name='__main__')
"""
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, str(Path(__file__).resolve().parents[1]),
             f"http://127.0.0.1:{server.server_port}", protocol],
            input=json.dumps({"document": HTML.hex()}), capture_output=True, text=True,
            timeout=15, env={**os.environ, "COMPANION_READER_TOKEN": TOKEN},
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    version = "v3" if protocol == "reading-v1" else "v2"
    assert all(path.startswith(f"/internal/{version}/readers/fixture/") for path, _, _ in received)
    assert all(auth == "Bearer " + TOKEN for _, auth, _ in received)
    if protocol == "legacy":
        assert len(received) == 1 and received[0][0].endswith("/lease")
    else:
        body = next(body for path, _, body in received if path.endswith("/result"))
        ReadObservation.model_validate(body)
        assert body["status"] == "SUCCESS" and body["used_requests"] == 1
        assert body["observations"][0]["provenance"] == item["context"]
        assert body["observations"][0]["trust"] == "untrusted_external"
        assert "恒星" in body["observations"][0]["text"]


@pytest.mark.parametrize("args", [
    ["--source", "web"],
    ["--source", "web", "--protocol", "reading-v1", "--probe", "https://articles.example/"],
    ["--source", "web", "--protocol", "reading-v1", "--allow-host", "*.example"],
])
def test_web_cli_rejects_implicit_protocol_self_authorized_probe_and_wildcards(args):
    result = subprocess.run([sys.executable, "-B", "-m", "source_reader", *args],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
