"""The real isolated Reader tick, mock API/CDN/Core HTTP, and real native media child."""

import base64
import copy
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpcore
import httpx
import pytest

from companion.bili_video_protocol import (
    DEFAULT_LIMITS,
    assets_from_wire,
    json_sha256,
    target_from_wire,
)
from source_reader import worker_v1
from source_reader.bili_tools import BilibiliAccountHTTP
from source_reader.bilibili import BilibiliReader
from source_reader.network import PublicHTTP, ReadFailure
from tests.conftest import run
from tests.support.bili_media import CDN, NAV, NOW, PLAY, VIEW, lease, pool
from tests.support.source_reader import response
from tests.support.video_extract import local_media
from tests.support.video_media_contract import clips, preview
from tests.test_plugin_readonly import SECRET, grant


class MediaCore:
    def __init__(self, item, *, use_clips=True, lose_ack=None, corrupt=None,
                 deny_after_prepare=False):
        self.item = item
        self.frozen_item = copy.deepcopy(item)
        self.use_clips, self.lose_ack, self.corrupt = use_clips, lose_ack, corrupt
        self.deny_after_prepare = deny_after_prepare
        self.prepared = None
        self.receipts, self.uploads, self.results, self.bodies, self.paths = {}, [], [], [], []
        self.prepare_count, self.receipt_count = 0, 0

    async def handle(self, request):
        path, body = request.url.path, json.loads(request.content)
        self.paths.append(path)
        self.bodies.append(body)
        if path.endswith("/lease"):
            assert body.get("capabilities") == ["bounded_video_media_v1"]
            value, self.item = self.item, None
            return httpx.Response(200, json={"item": value})
        if path.endswith("/validate"):
            if self.prepared is not None and self.deny_after_prepare:
                return httpx.Response(409, json={"error": "SOURCE_ERASED"})
            return httpx.Response(200, json={"valid": True})
        assert body["lease_sha256"] == json_sha256(self.frozen_item)
        if path.endswith("/media/receipt"):
            self.receipt_count += 1
            saved = self.receipts.get((body["phase"], body["request_sha256"]))
            return httpx.Response(200, json={"state": "FOUND", "response": saved}
                                  if saved else {"state": "NOT_FOUND"})
        if path.endswith("/media/result"):
            self.results.append(body)
            return httpx.Response(200, json={"accepted": True})
        if path.endswith("/media/prepare"):
            self.prepare_count += 1
            media = self.frozen_item["media"]
            self.target = target_from_wire({
                **{key: media[key] for key in (
                    "content_item_id", "source_event_id", "source_sha256", "bvid", "cid", "part",
                )}, "canonical_url": self.frozen_item["url"],
                "duration_seconds": body["identity"]["duration_seconds"],
            })
            self.selected = preview(self.target)
            result = {"prepared": True, "prepare_sha256": json_sha256(body),
                      "target": asdict(self.target), "selection": asdict(self.selected),
                      "limits": media["limits"], "deadline": media["deadline"]}
            if self.corrupt == "prepare_target":
                result["target"]["cid"] += 1
            self.prepared = result
            phase = "prepare"
        else:
            assert path.endswith("/media") and self.prepared is not None
            assert body["prepare_sha256"] == self.prepared["prepare_sha256"]
            assert body["selection_sha256"] == self.selected.sha256
            assert body["download"]["source_range"] == {"start_seconds": 0, "end_seconds": 3.0}
            assets = assets_from_wire(body["assets"], self.target, self.selected, DEFAULT_LIMITS,
                                      input_sha256=body["download"]["input_sha256"])
            self.uploads.append((body, assets))
            phase = body["stage"]
            following = clips(self.target) if phase == "preview" and self.use_clips else None
            result = {"accepted": True, "receipt_sha256": json_sha256(body),
                      "next_selection": asdict(following) if following else None}
            if following:
                self.selected = following
            if self.corrupt == "next_plan" and following:
                result["next_selection"]["plan_sha256"] = "c" * 64
        self.receipts[phase, json_sha256(body)] = result
        if phase == self.lose_ack:
            raise httpx.ReadTimeout("receipt persisted before connection loss")
        if self.lose_ack == "missing" and phase == "preview":
            self.receipts.clear()
            raise httpx.ReadTimeout("unknown disposition")
        return httpx.Response(200, json=result)

    def client(self):
        return httpx.AsyncClient(base_url="http://core", transport=httpx.MockTransport(self.handle))


async def scenario(tmp_path, *, use_clips=True, lose_ack=None, corrupt=None,
                   deny_after_prepare=False, download=None, account=False):
    _, artifact = local_media(tmp_path)
    native = (tmp_path / "source.mp4").read_bytes()
    upstream, backend = pool(response(VIEW), response(NAV), response(PLAY),
                             response(native) if download is None else download)
    public = (BilibiliAccountHTTP(grant(tmp_path, "bilibili", now=NOW, authorization={
        "grant_id": "grant_" + "1" * 32, "epoch": 1, "fingerprint": "2" * 64,
    }), upstream, clock=lambda: NOW) if account else PublicHTTP(upstream))
    async with public:
        item = lease()
        if account:
            item["source_grant"] = public.grant_snapshot(required=True)
        core = MediaCore(item, use_clips=use_clips, lose_ack=lose_ack, corrupt=corrupt,
                         deny_after_prepare=deny_after_prepare)
        async with core.client() as client:
            reader = BilibiliReader(public)
            assert await worker_v1.tick(
                client, reader, "fixture", source="bilibili", media_limits=DEFAULT_LIMITS,
                media_work_dir=tmp_path / "reader-work", clock=lambda: NOW,
            )
    return core, backend, artifact


@pytest.mark.parametrize("use_clips", [False, True])
def test_real_worker_downloads_once_and_materializes_only_core_selected_media(tmp_path, use_clips):
    core, backend, artifact = run(scenario(tmp_path, use_clips=use_clips))
    result = core.results[0]
    assert result["status"] == "SUCCESS" and result["used_requests"] == 4
    assert result["used_bytes"] == result["observed_bytes"]
    assert result["used_bytes"] > artifact.byte_length
    assert len(core.uploads) == (2 if use_clips else 1)
    frames = core.uploads[0][1]
    assert [asset.manifest.actual_start_seconds for asset in frames] == [0.2, 2.1]
    assert all(asset.manifest.output_duration_seconds == 0 for asset in frames)
    if use_clips:
        clip, = core.uploads[1][1]
        assert clip.manifest.actual_start_seconds == 0.3
        assert clip.manifest.actual_end_seconds == 1.2
        assert clip.manifest.codec == "h264" and clip.manifest.frame_count == 10
    assert len(backend.requests) == 4 and backend.server_names[-1] == "cn-test.bilivideo.com"
    serialized = json.dumps(core.bodies)
    assert CDN not in serialized and "token=transient" not in serialized
    assert str(tmp_path) not in serialized and "source.bin" not in serialized
    assert list((tmp_path / "reader-work").iterdir()) == []


@pytest.mark.parametrize("phase", ["prepare", "preview", "clips"])
def test_lost_ack_queries_exact_receipt_without_repeating_download_or_phase(tmp_path, phase):
    core, backend, _ = run(scenario(tmp_path, lose_ack=phase))
    assert core.results[0]["status"] == "SUCCESS"
    assert core.prepare_count == 1 and core.receipt_count == 1
    assert len(core.uploads) == 2 and len(backend.requests) == 4
    assert [body["phase"] for body in core.bodies if "phase" in body] == [phase]


def test_missing_ack_receipt_is_unknown_and_does_not_run_clip_phase(tmp_path):
    core, backend, _ = run(scenario(tmp_path, lose_ack="missing"))
    result = core.results[0]
    assert result["status"] == "UNKNOWN" and result["error_code"] == "MEDIA_RECEIPT_UNKNOWN"
    assert result["used_requests"] is None and result["used_bytes"] is None
    assert result["observed_requests"] == 4 and len(core.uploads) == 1
    assert core.receipt_count == 1 and len(backend.requests) == 4
    assert list((tmp_path / "reader-work").iterdir()) == []


def test_account_cookie_reaches_fixed_apis_but_never_cdn_or_core(tmp_path):
    core, backend, _ = run(scenario(tmp_path, account=True))
    assert core.results[0]["status"] == "SUCCESS"
    assert all(SECRET.encode() in request for request in backend.requests[:3])
    assert SECRET.encode() not in backend.requests[3]
    assert b"cookie:" not in backend.requests[3].lower()
    assert SECRET not in json.dumps(core.bodies)


@pytest.mark.parametrize(("corrupt", "requests", "uploads"), [
    ("prepare_target", 3, 0), ("next_plan", 4, 1),
])
def test_core_response_cannot_rebind_target_or_invent_a_new_plan(
    tmp_path, corrupt, requests, uploads,
):
    core, backend, _ = run(scenario(tmp_path, corrupt=corrupt))
    assert core.results[0]["status"] == "FAILED"
    assert len(backend.requests) == requests and len(core.uploads) == uploads


def test_erasure_after_prepare_stops_before_media_download(tmp_path):
    core, backend, _ = run(scenario(tmp_path, deny_after_prepare=True))
    result = core.results[0]
    assert result["status"] == "CANCELLED" and result["error_code"] == "SOURCE_ERASED"
    assert result["used_requests"] is None and result["observed_requests"] == 3
    assert len(backend.requests) == 3 and not core.uploads


def test_cdn_timeout_retains_unknown_usage_and_does_not_repeat_provider_io(tmp_path):
    core, backend, _ = run(scenario(tmp_path, download=httpcore.ReadTimeout("uncertain CDN")))
    result = core.results[0]
    assert result["status"] == "UNKNOWN" and result["used_requests"] is None
    assert result["observed_requests"] == 4 and not core.uploads
    assert len(backend.hosts) == 4 and not backend.rows


def test_cdn_redirect_is_not_followed(tmp_path):
    core, backend, _ = run(scenario(tmp_path, download=response(b"", status=302, headers={
        "Location": "https://cn-another.bilivideo.com/second.mp4",
    })))
    assert core.results[0]["status"] == "FAILED"
    assert len(backend.requests) == 4 and not core.uploads


def test_unsolicited_media_lease_rejected_before_any_upstream_request(tmp_path):
    async def check():
        upstream, backend = pool()
        async with PublicHTTP(upstream) as public:
            # This mock intentionally supplies an unsolicited media lease to an old worker.
            async def legacy_lease(request):
                return httpx.Response(200, json={"item": lease()})

            async with httpx.AsyncClient(base_url="http://core",
                                         transport=httpx.MockTransport(legacy_lease)) as legacy:
                with pytest.raises(ReadFailure, match="READER_LEASE_INVALID"):
                    await worker_v1.tick(legacy, BilibiliReader(public), "fixture",
                                         source="bilibili", clock=lambda: NOW)
            assert not backend.requests

    run(check())


def test_independent_cli_process_uses_real_core_http_and_only_shared_stdlib_contracts(tmp_path):
    local_media(tmp_path)
    native = (tmp_path / "source.mp4").read_bytes()
    fixtures = tmp_path / "upstream.json"
    fixtures.write_text(json.dumps([base64.b64encode(row).decode() for row in (
        response(VIEW), response(NAV), response(PLAY), response(native),
    )]), encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    isolated = tmp_path / "isolated"
    shutil.copytree(root / "source_reader", isolated / "source_reader",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (isolated / "companion").mkdir()
    for name in ("__init__.py", "source_policy.py", "video_media_contract.py",
                 "bili_video_protocol.py"):
        shutil.copy2(root / "companion" / name, isolated / "companion" / name)
    item = lease()
    deadline = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()
    item["lease_until"] = item["media"]["deadline"] = deadline
    core = MediaCore(item)
    failures = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                assert self.headers["Authorization"] == "Bearer independent-media-reader-token"
                request = httpx.Request("POST", "http://core" + self.path, content=body)
                value = run(core.handle(request))
                encoded = value.content
                self.send_response(value.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except Exception as error:
                failures.append(error)
                self.send_error(500)

        def log_message(self, *args):
            pass

    script = r'''
import base64
import importlib.abc
import json
import runpy
import ssl
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fastapi', 'pydantic', 'alembic', 'sqlite3', 'fastembed'}:
            raise ImportError('FORBIDDEN_READER_DEPENDENCY:' + fullname)
        if fullname.startswith('companion.') and fullname not in {
            'companion.source_policy', 'companion.video_media_contract',
            'companion.bili_video_protocol',
        }:
            raise ImportError('FORBIDDEN_CORE_MODULE:' + fullname)
sys.meta_path.insert(0, Boundary())
import httpcore
from source_reader.network import PublicHTTP, PublicNetwork

rows = [base64.b64decode(row) for row in json.loads(Path(sys.argv[2]).read_text('utf-8'))]
requests = []
record = Path(sys.argv[3])
class Stream(httpcore.AsyncMockStream):
    async def write(self, buffer, timeout=None):
        if buffer:
            requests.append(buffer.decode('latin1'))
    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        assert ssl_context.check_hostname
        return self
class Backend(httpcore.AsyncNetworkBackend):
    async def connect_tcp(self, host, port, **kwargs):
        assert host == '93.184.216.34'
        return Stream([rows.pop(0)])
async def addresses(host, port):
    return ['93.184.216.34']
original = PublicHTTP.__init__
def init(self, pool=None):
    original(self, httpcore.AsyncConnectionPool(
        ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        network_backend=PublicNetwork(resolver=addresses, backend=Backend()),
        max_keepalive_connections=0, retries=0,
    ))
PublicHTTP.__init__ = init
sys.argv = ['source_reader', '--source', 'bilibili', '--protocol', 'reading-v1',
            '--video-media', '--core-url', sys.argv[4], '--adapter-id', 'fixture', '--once']
try:
    runpy.run_module('source_reader', run_name='__main__')
finally:
    record.write_text(json.dumps({'requests':requests, 'remaining':len(rows)}), encoding='utf-8')
'''
    record = tmp_path / "network-receipt.json"
    work = tmp_path / "reader-work"
    work.mkdir()
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", script, str(isolated),
                 str(fixtures), str(record),
                 "http://127.0.0.1:" + str(server.server_port)],
                cwd=isolated, env={**os.environ, "TMPDIR": str(work),
                                  "COMPANION_READER_TOKEN": "independent-media-reader-token"},
                capture_output=True, text=True, timeout=45, check=False,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert not failures, failures
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(core.results) == 1 and core.results[0]["status"] == "SUCCESS", result.stdout
    assert core.results[0]["used_requests"] == 4 and len(core.uploads) == 2
    actual = json.loads(record.read_text("utf-8"))
    assert len(actual["requests"]) == 4 and actual["remaining"] == 0
    assert all("independent-media-reader-token" not in request for request in actual["requests"])
    assert list(work.iterdir()) == []
