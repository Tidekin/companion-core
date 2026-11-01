"""Real inbox HTTP, native Core lease/commit, and an isolated Reader CLI process."""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import zlib
from pathlib import Path

import httpx
import pytest
import uvicorn

from companion.api import create_app
from companion.config import ObservationAdapterConfig
from companion.core import Core
from tests.conftest import event, run
from tests.sdk_reader_fixture import Provider, answer, call, worker_for
from tests.support.native_consumer_entry import environment
from tests.support.native_http import owned_http_protocol
from tests.test_next_reading import HTML, WEB
from tests.test_onebot_capture import real_core_http
from tests.test_reading_protocol import envelope
from tests.test_reading_service import TOKEN, configure
from tests.test_source_reader import ITEM, META, PLAYER, SUBTITLE


@pytest.mark.parametrize("hosts", [("articles.example",), ("*",)])
def test_inbox_to_independent_reader_cli_then_restart_cannot_reexecute(core, tmp_path, hosts):
    configure(core)
    core.settings.web_reading_hosts = hosts
    # A second actual Core owns HTTP; Reader has no database or Core-service access.
    with Core(core.settings) as http_core:
        app = create_app(core.settings)
        app.state.core = http_core
        bound = socket.socket()
        bound.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{bound.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off",
                                http=owned_http_protocol(lambda: server.servers)))
        ready = threading.Event()
        startup = server.startup

        async def started(sockets=None):
            await startup(sockets)
            ready.set()

        server.startup = started
        thread = threading.Thread(target=lambda: server.run(sockets=[bound]), daemon=True)
        thread.start()
        script = r'''
import importlib.abc
import json
import runpy
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])

class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'sqlite3', 'fastapi', 'pydantic', 'alembic'}:
            raise ImportError('FORBIDDEN_READER_DEPENDENCY:' + fullname)
        if fullname.startswith('companion.') and fullname != 'companion.source_policy':
            raise ImportError('FORBIDDEN_CORE_MODULE:' + fullname)

sys.meta_path.insert(0, Boundary())
import httpcore
from source_reader import worker_v1
from source_reader.web import WebHTTP

raw = bytes.fromhex(json.loads(sys.stdin.read())['document'])
wire = b'HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: '
wire += str(len(raw)).encode() + b'\r\nConnection: close\r\n\r\n' + raw
expected_hosts = set(json.loads(sys.argv[3]))

def provider(hosts):
    assert hosts == expected_hosts
    pool = httpcore.AsyncConnectionPool(network_backend=httpcore.AsyncMockBackend([wire]))
    return WebHTTP(hosts, pool)

worker_v1.WebHTTP = provider
origin = sys.argv[2]
sys.argv = ['source_reader', '--source', 'web', '--protocol', 'reading-v1',
            '--core-url', origin, '--adapter-id', 'web', '--once']
runpy.run_module('source_reader', run_name='__main__')
'''
        try:
            assert ready.wait(10), "Native HTTP server did not start"
            with httpx.Client(base_url=origin, trust_env=False) as client:
                admitted = client.post(
                    "/internal/v1/inbox/events", json=event(
                        http_core, text="请阅读 " + WEB,
                    ).model_dump(mode="json"),
                    headers={
                        "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value(),
                        "X-Idempotency-Key": "independent-reading-inbox",
                    },
                )
                assert admitted.status_code == 202, admitted.text
            children = []
            for _ in range(2):
                child = subprocess.run(
                    [sys.executable, "-I", "-c", script,
                     str(Path(__file__).resolve().parents[1]), origin, json.dumps(hosts)],
                    cwd=tmp_path, input=json.dumps({"document": HTML.hex()}),
                    env={**os.environ, "COMPANION_READER_TOKEN": TOKEN},
                    text=True, capture_output=True, timeout=30,
                )
                assert child.returncode == 0, child.stdout + child.stderr
                children.append(child)
            attempts = core.store.rows("SELECT * FROM reading_attempts")
            assert len(attempts) == 1 and attempts[0]["state"] == "DONE", children[0].stdout
            assert attempts[0]["observed_requests"] == 1
            observations = core.store.rows("SELECT * FROM observations")
            assert len(observations) == 1
            evidence = json.loads(observations[0]["evidence_json"])["read_observation"]
            assert evidence["provenance"]["source_event_ids"] == ["evt_test_1"]
            assert evidence["coverage"]["scope"] == "extracted_page_text"
            assert evidence["trust"] == "untrusted_external"
            body = core.store.one("SELECT body_json FROM events WHERE event_id=?",
                                  (observations[0]["event_id"],))
            assert "恒星" in json.loads(body["body_json"])["text"]
            assert not core.store.one("SELECT 1 FROM outbox")
            assert not core.store.one("SELECT 1 FROM model_usage")
        finally:
            server.should_exit = True
            thread.join(10)
            bound.close()
            assert not thread.is_alive()


def test_signed_private_plaintext_web_read_late_result_cannot_cross_stop(core, tmp_path):
    async def scenario():
        options = dict(
            content_enabled=True, observation_enabled=True, web_reading_enabled=True,
            exploration_enabled=True, web_reading_hosts=("articles.example",),
            observation_adapters={
                "web": ObservationAdapterConfig(source="web", token=TOKEN),
            }, exploration_daily_requests=8, exploration_monthly_requests=16,
            exploration_daily_bytes=4194304,
        )
        async with environment(core, tmp_path, purposes=("chat", "content", "exploration"),
                               core_options=options) as (entry, sends):
            client = entry.client
            await entry.admit(1, "请阅读 " + WEB)
            provider = Provider(answer(calls=[call("read-page", "read_content", {"url": WEB})]))
            async with worker_for(core, provider) as worker:
                assert await worker.run_once("native-shared-link-read")
            assert len(provider.requests) == 1
            usage = core.store.rows("SELECT * FROM model_usage")
            assert len(usage) == 1 and usage[0]["state"] == "SETTLED"
            auth = {"Authorization": "Bearer " + TOKEN}
            leased = await client.post("/internal/v3/readers/web/lease", json={}, headers=auth)
            assert leased.status_code == 200, leased.text
            item = leased.json()["item"]
            assert item and item["url"] == WEB
            await entry.stop()
            prefix = "/internal/v3/readers/web/" + item["request_id"]
            check = await client.post(prefix + "/validate", headers=auth,
                                      json={"lease_token": item["lease_token"]})
            assert check.status_code == 409
            body = envelope()
            body["lease_token"] = item["lease_token"]
            observation = body["observations"][0]
            observation["provenance"] = item["context"]
            observation["fetched_at"] = observation["evidence"]["fetched_at"] = core.clock()
            completed = await client.post(prefix + "/result", headers=auth, json=body)
            assert completed.status_code == 200, completed.text
            assert completed.json()["state"] == "CANCELLED"
            assert not completed.json()["event_ids"]
            assert not core.store.one("SELECT 1 FROM observations")
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert not sends

    run(scenario())


def test_bili_discussion_reader_cli_metering_context_and_restart(core, tmp_path):
    configure(core)
    core.settings.observation_adapters["bili"] = ObservationAdapterConfig(
        source="bilibili", token=TOKEN, reader_max_requests=5,
    )
    metadata = {**META, "data": {
        **META["data"], "aid": 1701, "bvid": ITEM["url"].rsplit("/", 1)[1], "duration": 12,
    }}
    comments = {"code": 0, "data": {"page": {"count": 12}, "replies": [{
        "rpid": 101, "floor": 2, "member": {"uname": "外部评论者"},
        "content": {"message": "评论层资料，不是字幕。"}, "replies": [{
            "rpid": 102, "parent": 101, "member": {"uname": "回复者"},
            "content": {"message": "这是一条楼中楼回复。"},
        }],
    }]}}
    danmaku = (
        '<i><chatid>1234</chatid><d p="1.25,1,25,0,0,0,fixture,1">弹幕样本一</d>'
        '<d p="4.0,1,25,0,0,0,fixture,2">弹幕样本二</d></i>'
    ).encode()
    # Meter the compressed bytes received, independently of decoded XML size.
    payloads = [json.dumps(value, ensure_ascii=False).encode()
                for value in (metadata, PLAYER, SUBTITLE, comments)] + [zlib.compress(danmaku)]
    roles = ["metadata", "player", "subtitle", "comments", "danmaku"]
    source_root = str(Path(__file__).resolve().parents[1])
    bridge_token = core.settings.bridge_token.get_secret_value()
    child_input = json.dumps({"payloads": [raw.hex() for raw in payloads]})
    script = r'''
import importlib.abc
import json
import runpy
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])

class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'sqlite3', 'fastapi', 'pydantic', 'alembic'}:
            raise ImportError('FORBIDDEN_READER_DEPENDENCY:' + fullname)
        if fullname.startswith('companion.') and fullname != 'companion.source_policy':
            raise ImportError('FORBIDDEN_CORE_MODULE:' + fullname)

sys.meta_path.insert(0, Boundary())
import httpcore
import ssl
from source_reader import network

payloads = [bytes.fromhex(value) for value in json.loads(sys.stdin.read())['payloads']]
responses = []
for raw in payloads:
    responses.append(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(raw)).encode()
                     + b'\r\nConnection: close\r\n\r\n' + raw)

class Stream(httpcore.AsyncMockStream):
    async def write(self, buffer, timeout=None):
        if buffer:
            assert buffer.startswith(b'GET '), buffer
            assert b'authorization:' not in buffer.lower() and b'cookie:' not in buffer.lower()
            backend.requests.append(buffer.split(b'\r\n', 1)[0].decode())

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        assert ssl_context.check_hostname
        backend.server_names.append(server_hostname)
        return self

class Backend(httpcore.AsyncNetworkBackend):
    def __init__(self):
        self.requests, self.server_names = [], []

    async def connect_tcp(self, host, port, **kwargs):
        assert host == '93.184.216.34' and port == 443
        return Stream([responses.pop(0)])

async def public_addresses(host, port):
    assert host in {'api.bilibili.com', 'aisubtitle.hdslb.com'} and port == 443
    return ['93.184.216.34']

backend = Backend()

class ProviderHTTP(network.PublicHTTP):
    def __init__(self, pool=None):
        # Mock TLS must not synchronously scan host CA files on each fake connection.
        pool = pool or httpcore.AsyncConnectionPool(
            ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            network_backend=network.PublicNetwork(resolver=public_addresses, backend=backend),
            max_connections=1, max_keepalive_connections=0, retries=0,
        )
        super().__init__(pool)

network.PublicHTTP = ProviderHTTP
origin = sys.argv[2]
sys.argv = ['source_reader', '--source', 'bilibili', '--protocol', 'reading-v1',
            '--core-url', origin, '--adapter-id', 'bili', '--discussion', '--once']
runpy.run_module('source_reader', run_name='__main__')
assert not {'sqlite3', 'fastapi', 'pydantic', 'alembic'} & sys.modules.keys()
assert not [name for name in sys.modules
            if name.startswith('companion.') and name != 'companion.source_policy']
print(json.dumps({'provider_requests': backend.requests, 'server_names': backend.server_names,
                  'unused_responses': len(responses)}))
'''

    async def scenario():
        with Core(core.settings) as http_core:
            async with real_core_http(http_core) as port:
                origin = f"http://127.0.0.1:{port}"
                async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
                    admitted = await client.post(
                        "/internal/v1/inbox/events", json=event(
                            http_core, text="请阅读这个视频的字幕和评论弹幕 " + ITEM["url"],
                        ).model_dump(mode="json"),
                        headers={
                            "Authorization": "Bearer " + bridge_token,
                            "X-Idempotency-Key": "independent-bili-discussion-inbox",
                        },
                    )
                    assert admitted.status_code == 202, admitted.text
                children, snapshots = [], []
                for _ in range(2):
                    child = await asyncio.to_thread(
                        subprocess.run,
                        [sys.executable, "-I", "-c", script, source_root, origin],
                        cwd=tmp_path, input=child_input,
                        env={**os.environ, "COMPANION_READER_TOKEN": TOKEN},
                        text=True, capture_output=True, timeout=30,
                    )
                    assert child.returncode == 0, child.stdout + child.stderr
                    children.append(json.loads(child.stdout.splitlines()[-1]))
                    snapshots.append((
                        core.store.rows("SELECT * FROM reading_attempts"),
                        core.store.rows("SELECT * FROM exploration_requests"),
                        core.store.rows("SELECT * FROM observations"),
                    ))
                assert snapshots[0] == snapshots[1]  # Restart cannot reread or charge again.
                requests = children[0]["provider_requests"]
                diagnostic = {
                    "provider": children[0],
                    "attempts": [{key: row[key] for key in (
                        "state", "observed_requests", "observed_bytes",
                    )} for row in snapshots[0][0]],
                    "settlement": [{key: row[key] for key in (
                        "state", "used_requests", "used_bytes", "last_error_code",
                    )} for row in snapshots[0][1]],
                }
                assert len(requests) == 5 and children[0]["unused_responses"] == 0, diagnostic
                assert [request.split("?", 1)[0] for request in requests] == [
                    "GET /x/web-interface/view", "GET /x/player/wbi/v2",
                    "GET /bfs/subtitle/example.json", "GET /x/v2/reply", "GET /x/v1/dm/list.so",
                ]
                assert children[1]["provider_requests"] == []
                assert children[1]["unused_responses"] == 5
                assert children[0]["server_names"] == [
                    "api.bilibili.com", "api.bilibili.com", "aisubtitle.hdslb.com",
                    "api.bilibili.com", "api.bilibili.com",
                ]
                attempts, exploration, observations = snapshots[0]
                assert len(attempts) == len(exploration) == len(observations) == 1
                assert attempts[0]["state"] == exploration[0]["state"] == "DONE"
                assert attempts[0]["observed_requests"] == exploration[0]["used_requests"] == 5
                assert attempts[0]["observed_bytes"] == exploration[0]["used_bytes"] == sum(
                    len(raw) for raw in payloads
                )
                stored = json.loads(observations[0]["evidence_json"])["read_observation"]
                assert stored["parser_version"] == "bilibili/4"
                assert stored["provenance"]["source_event_ids"] == ["evt_test_1"]
                assert stored["coverage"]["scope"] == "first_part_subtitles"
                assert stored["coverage"]["complete_video"] is False
                assert stored["coverage"]["video_analyzed"] is False
                assert stored["coverage"]["audio_analyzed"] is False
                evidence = stored["evidence"]
                assert evidence["discussion_status"] == {
                    "comments": "available", "danmaku": "available",
                }
                assert [row["role"] for row in evidence["sources"]] == roles
                for source, raw in zip(evidence["sources"], payloads, strict=True):
                    assert source["bytes"] == len(raw)
                    assert source["sha256"] == hashlib.sha256(raw).hexdigest()
                    assert "?" not in source["url"]
                assert evidence["comments"]["reported_total"] == 12
                assert evidence["comments"]["partial"] is True
                comment_ids = [row["comment_id"] for row in evidence["comments"]["items"]]
                assert comment_ids == ["101", "102"]
                assert evidence["comments"]["items"][1]["reply_to_id"] == "101"
                assert evidence["danmaku"] == {
                    "cid": 1234, "scope": "current_pool_sample", "partial": True,
                    "items": [{"at_seconds": 1.25, "text": "弹幕样本一"},
                              {"at_seconds": 4.0, "text": "弹幕样本二"}],
                }
                context = json.loads(http_core.content.context_message(
                    "evt_test_1", 16000,
                ))["shared_content"]
                parts = {row["kind"]: row for row in context}
                assert set(parts) == {"text", "comments", "danmaku"}
                assert "恒星的颜色与温度有关" in parts["text"]["text"]
                assert "评论层资料" not in parts["text"]["text"]
                assert "弹幕样本" not in parts["text"]["text"]
                assert "评论层资料" in parts["comments"]["text"]
                assert parts["comments"]["comment_scope"]["partial"] is True
                assert "弹幕样本一" in parts["danmaku"]["text"]
                assert parts["danmaku"]["danmaku_scope"]["scope"] == "current_pool_sample"
                assert parts["danmaku"]["danmaku_scope"]["partial"] is True
                assert parts["text"]["trust"] == "untrusted_external"
                assert parts["comments"]["trust"] == "external_untrusted"
                assert parts["danmaku"]["trust"] == "external_untrusted"
                assert not core.store.one("SELECT 1 FROM outbox")
                assert not core.store.one("SELECT 1 FROM model_usage")

    run(scenario())
