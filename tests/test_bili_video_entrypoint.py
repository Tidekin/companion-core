"""Real HTTP/capture and independent workers; only external transports are synthetic."""

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import httpx
import pytest

from companion import __main__, cloud_budget
from companion import core as core_module
from companion.core import Core
from companion.model import ModelClient
from companion.retrieval import SemanticIndex
from tests.conftest import run
from tests.sdk_reader_fixture import answer, call
from tests.support.native_consumer import source_window
from tests.support.onebot_events import raw_event
from tests.test_bili_media import NAV, PLAY, VIEW
from tests.test_bili_video import (
    model_response,
    paid_media,
    paid_options,
    phase_costs,
    phase_usage,
    receipt,
    upload_body,
)
from tests.test_cloud_budget import native
from tests.test_onebot_capture import real_core_http
from tests.test_source_reader import PLAYER, response
from tests.test_video_extract import local_media

BVID = "BV1xx411c7mD"
URL = "https://www.bilibili.com/video/" + BVID
TOKEN = "synthetic-bili-reader-token-123456"

READER = r'''
import base64, importlib.abc, json, runpy, ssl, sys
from datetime import datetime
sys.path.insert(0, sys.argv[1])
class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'sqlite3', 'fastapi', 'pydantic', 'alembic', 'fastembed'}:
            raise ImportError('FORBIDDEN_READER_DEPENDENCY:' + fullname)
        if fullname.startswith('companion.') and fullname not in {
            'companion.source_policy', 'companion.video_media_contract',
            'companion.bili_video_protocol',
        }:
            raise ImportError('FORBIDDEN_CORE_MODULE:' + fullname)
sys.meta_path.insert(0, Boundary())
import httpcore
from source_reader import worker_v1
from source_reader.bilibili import BilibiliReader
from source_reader.network import PublicHTTP, PublicNetwork
value = json.loads(sys.stdin.read())
native_reader = BilibiliReader.__init__
def initialize_reader(self, *args, **kwargs):
    native_reader(self, *args, clock=lambda: value['now'], **kwargs)
BilibiliReader.__init__ = initialize_reader
rows = [base64.b64decode(row) for row in value['responses']]
seen = []
class Stream(httpcore.AsyncMockStream):
    async def write(self, buffer, timeout=None):
        if buffer:
            assert b'authorization:' not in buffer.lower()
            assert b'cookie:' not in buffer.lower()
            seen.append(buffer.decode('latin1'))
    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        assert ssl_context.check_hostname
        return self
class Backend(httpcore.AsyncNetworkBackend):
    async def connect_tcp(self, host, port, **kwargs):
        assert host == '93.184.216.34' and port == 443
        return Stream([rows.pop(0)])
async def addresses(host, port):
    assert host in {'api.bilibili.com', 'aisubtitle.hdslb.com', 'cn-test.bilivideo.com'}
    return ['93.184.216.34']
original = PublicHTTP.__init__
def init(self, pool=None):
    original(self, httpcore.AsyncConnectionPool(
        ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        network_backend=PublicNetwork(resolver=addresses, backend=Backend()),
        max_keepalive_connections=0, retries=0,
    ))
PublicHTTP.__init__ = init
native_tick = worker_v1.tick
async def tick(*args, **kwargs):
    return await native_tick(*args, clock=lambda: datetime.fromisoformat(value['now']), **kwargs)
worker_v1.tick = tick
sys.argv = ['source_reader', '--source', 'bilibili', '--protocol', 'reading-v1',
            '--core-url', sys.argv[2], '--adapter-id', 'bili', '--once',
            '--video-media' if value['media'] else '--discussion']
try:
    runpy.run_module('source_reader', run_name='__main__')
finally:
    print(json.dumps({'requests': seen, 'remaining': len(rows)}))
'''


def reader_files(tmp_path):
    source = Path(__file__).resolve().parents[1]
    isolated = tmp_path / "reader"
    shutil.copytree(source / "source_reader", isolated / "source_reader",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (isolated / "companion").mkdir()
    for filename in ("__init__.py", "source_policy.py", "video_media_contract.py",
                     "bili_video_protocol.py"):
        shutil.copy2(source / "companion" / filename, isolated / "companion" / filename)
    return isolated


async def reader_once(root, origin, clock, rows, *, media=False):
    work = root / "owned-temp"
    work.mkdir(exist_ok=True)
    completed = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-I", "-B", "-c", READER, str(root), origin],
        cwd=root, input=json.dumps({"responses": [base64.b64encode(row).decode() for row in rows],
                                   "now": clock(), "media": media}),
        env={**os.environ, "COMPANION_READER_TOKEN": TOKEN, "TMPDIR": str(work)},
        text=True, capture_output=True, timeout=45, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert list(work.iterdir()) == [], completed.stdout + completed.stderr
    return json.loads(completed.stdout.splitlines()[-1])


def upstream(tmp_path):
    local_media(tmp_path)
    view = {"code": 0, "data": {**VIEW["data"], "bvid": BVID, "aid": 1701,
                                "title": "Synthetic star colors"}}
    subtitle = {"body": [{"from": 0, "to": 2.5, "content": "字幕说恒星颜色与温度有关。"}]}
    comments = {"code": 0, "data": {"page": {"count": 20}, "replies": [{
        "rpid": 101, "floor": 1, "member": {"uname": "synthetic reader"},
        "content": {"message": "评论资料：我喜欢蓝色。"},
    }]}}
    danmaku = '<i><chatid>123</chatid><d p="1.25,1,25,0,0,0,x,1">弹幕资料</d></i>'
    return (
        [response(value) for value in (view, PLAYER, subtitle, comments)]
        + [response(zlib.compress(danmaku.encode()))],
        [response(view), response(NAV), response(PLAY),
         response((tmp_path / "source.mp4").read_bytes())],
    )


@pytest.mark.parametrize("erase_during_model", [False, True])
def test_capture_real_http_independent_reader_and_worker_preserve_bounded_evidence(
    core, tmp_path, monkeypatch, erase_during_model,
):
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    calls, instances, sends = [], [], []
    isolated = reader_files(tmp_path)
    metadata, media = upstream(tmp_path)

    async def source(ws):
        await ws.send(json.dumps(raw_event(time=int(core.clock.now.timestamp()),
            text="看看字幕、评论和画面 " + URL)))
        await ws.wait_closed()

    def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["model"] == "synthetic-bili-video":
            if erase_during_model:
                owner = core.store.one("SELECT owner_event_id FROM bili_video_items")
                with core.store.transaction():
                    core.ledger.expire_events(core.store, [owner["owner_event_id"]], core.clock())
            count = sum(row["model"] == "synthetic-bili-video" for row in calls)
            return model_response({"analysis": "采样帧有蓝色区域", "need_clips": True}
                                  if count == 1 else {"analysis": "采样片段里颜色逐渐变红"})
        chat_count = sum(row["model"] != "synthetic-bili-video" for row in calls)
        if chat_count <= 2:
            tool = "read_content" if chat_count == 1 else "analyze_shared_video"
            return httpx.Response(200, json=answer(calls=[call(tool, tool, {"url": URL})]))
        return httpx.Response(200, json={"choices": [{"message": {"content":
            "我只看了局部采样，采样片段的颜色逐渐变红。"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    def construct(*args, **kwargs):
        instance = Core(*args, clock=core.clock, **kwargs)
        instances.append(instance)
        return instance

    async def scenario():
        finished, read_ready, video_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
        native_completed = __main__.Recorder.completed

        def observed_completion(recorder, **kwargs):
            native_completed(recorder, **kwargs)
            if core.store.one("SELECT 1 FROM outbox"):
                finished.set()
            if core.store.one("SELECT 1 FROM exploration_requests"):
                read_ready.set()
            if core.store.one("SELECT 1 FROM bili_video_items"):
                video_ready.set()

        monkeypatch.setattr(__main__.Recorder, "completed", observed_completion)
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model_http:
            async with source_window(core, tmp_path, source,
                scope_options=paid_options(core, call_limit=5),
                model=ModelClient(core.settings, model_http)) as (
                    window, client, protocol, _, bridge_sends):
                cfg = window.config
                core.content.options = core.settings.content
                core.model = ModelClient(core.settings, model_http)
                monkeypatch.setattr(core_module, "Core", construct)
                monkeypatch.setattr(__main__, "ModelClient", lambda settings, _: ModelClient(
                    settings, model_http))
                await window.capture_once(client, protocol)
                assert await window.runtime.flush_one(client)
                worker = asyncio.create_task(__main__.worker(core.settings))
                try:
                    await asyncio.wait_for(read_ready.wait(), 15)
                    first = await reader_once(isolated, cfg.core_url, core.clock, metadata)
                    assert len(first["requests"]) == 5 and first["remaining"] == 0
                    assert not core.store.one("SELECT 1 FROM outbox")
                    await asyncio.wait_for(video_ready.wait(), 15)
                    assert core.store.one("SELECT state FROM bili_video_items") == {
                        "state": "DECLARED"
                    }, {"attempts": core.store.rows(
                            "SELECT state,observed_requests FROM reading_attempts"),
                        "content": core.store.rows(
                            "SELECT state,error_code FROM content_items"),
                        "audit": core.store.rows(
                            "SELECT action,safe_detail_json FROM audit_log "
                            "ORDER BY rowid DESC LIMIT 4")}
                    second = await reader_once(
                        isolated, cfg.core_url, core.clock, media, media=True,
                    )
                    assert len(second["requests"]) == 4 and second["remaining"] == 0
                    if not erase_during_model:
                        await asyncio.wait_for(finished.wait(), 8)
                    snapshot = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
                    restart = await reader_once(
                        isolated, cfg.core_url, core.clock, media, media=True,
                    )
                    assert restart == {"requests": [], "remaining": 4}
                    assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == (
                        snapshot
                    )
                finally:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                sends.extend(bridge_sends)

    run(scenario())
    assert len(instances) == 1 and instances[0].store is not core.store
    assert sends == []
    row = core.store.one("SELECT * FROM bili_video_items")
    usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
    video = [entry for entry in usage if entry["scenario"].startswith("content_video")]
    assert len(video) == (1 if erase_during_model else 2)
    assert all(entry["state"] == "SETTLED" for entry in video)
    costs = phase_costs(core)
    assert {entry["task_id"] for entry in costs} == {"content:" + row["item_id"]}
    assert sum(entry["amount_micro_cny"] for entry in costs) <= 1_000_000
    policies = [json.loads(entry["snapshot_json"])["config"] for entry in costs]
    assert all(policy["video_per_item_micro_cny"] == 1_000_000 for policy in policies)
    assert all(policy["daily_micro_cny"] == 20_000_000 for policy in policies)
    if erase_during_model:
        assert row["redacted"] == 1 and row["output_event_id"] is None
        assert row["lease_json"] == row["target_json"] == row["plan_json"] == "{}"
        assert core.bili_video.context(row["item_id"]) is None
        assert not core.store.one("SELECT 1 FROM outbox")
        assert len(calls) == 3
        return
    assert row["state"] == "COMPLETE"
    attempts = core.store.rows(
        "SELECT state,observed_requests FROM reading_attempts ORDER BY rowid"
    )
    assert attempts == [{"state": "DONE", "observed_requests": 5},
                        {"state": "DONE", "observed_requests": 4}]
    assert [entry["scenario"] for entry in usage] == [
        "chat", "chat", "content_video_preview", "content_video", "chat",
    ]
    final = json.dumps(calls[-1], ensure_ascii=False)
    assert "采样片段里颜色逐渐变红" in final, {
        "output_event_id": row["output_event_id"],
        "output_has_clip_analysis": "采样片段里颜色逐渐变红" in core.store.one(
            "SELECT body_json FROM events WHERE event_id=?",
            (row["output_event_id"],))["body_json"],
        "output_linked_to_request": bool(core.store.one(
            "SELECT 1 FROM exploration_evidence WHERE request_id=? AND event_id=?",
            (row["request_id"], row["output_event_id"]))),
        "tool_items": [json.loads(message["content"])["items"] for message in calls[-1]["messages"]
                       if message.get("tool_call_id") == "analyze_shared_video"],
    }
    assert "恒星颜色与温度" in final and "评论资料" in final and "弹幕资料" in final
    assert "token=transient" not in final and "base64" not in final
    context = json.loads(core.content.context_message(row["owner_event_id"], 30000))
    entries = context["shared_content"]
    visual = next(entry for entry in entries if entry["kind"] == "bili_video")
    assert visual["bili_video"]["partial"] is True
    assert visual["bili_video"]["complete_video"] is False
    assert visual["bili_video"]["audio_analyzed"] is False
    assert len(visual["bili_video"]["samples"]) == 2
    assert any(sample["frames_seconds"] for sample in visual["bili_video"]["samples"])
    assert any(sample["clips_seconds"] for sample in visual["bili_video"]["samples"])
    assert not core.store.one("SELECT 1 FROM observations WHERE result_status='WATCHED'")


def test_real_http_unknown_phase_receipt_cannot_repay_after_restart(core, tmp_path):
    calls = []

    def provider(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic provider acknowledgement lost")

    async def scenario():
        async with paid_media(core, tmp_path, provider) as (_, item, prepared):
            body = upload_body(item, prepared)
            async with real_core_http(core) as port:
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}", trust_env=False,
                    headers={"Authorization": "Bearer " + TOKEN},
                ) as client:
                    prefix = "/internal/v3/readers/bili/" + item["request_id"] + "/media"
                    response = await client.post(prefix, json=body)
                    assert response.status_code == 503, response.text
                    usage = phase_usage(core)
                    assert len(usage) == 1 and usage[0]["state"] == "UNKNOWN"
                    assert usage[0]["billed_micro_usd"] > 0
                    query = receipt(item, "preview", body)
                    result = await client.post(prefix + "/receipt", json=query)
                    assert result.json() == {"state": "PENDING"}
                    again = await client.post(prefix, json=body)
                    assert again.status_code in {409, 503}, again.text
                    assert phase_usage(core) == usage
                    assert len(calls) == 1
                    costs = phase_costs(core)
                    assert len(costs) == 1 and costs[0]["state"] == "UNKNOWN"
                    assert costs[0]["amount_micro_cny"] > 0
            with Core(core.settings, core.model, clock=core.clock) as restarted:
                async with real_core_http(restarted) as port:
                    async with httpx.AsyncClient(
                        base_url=f"http://127.0.0.1:{port}", trust_env=False,
                        headers={"Authorization": "Bearer " + TOKEN},
                    ) as client:
                        checked = await client.post(prefix + "/receipt", json=query)
                        assert checked.json() == {"state": "PENDING"}
                        rejected = await client.post(prefix, json=body)
                        assert rejected.status_code in {409, 503}, rejected.text
                        assert phase_usage(restarted) == usage
                        assert len(calls) == 1

    run(scenario())


@pytest.mark.parametrize("ceiling", ["item", "day"])
def test_real_http_clip_phase_obeys_original_one_cny_and_twenty_cny_ceiling(
    core, tmp_path, ceiling,
):
    calls = []

    def provider(request):
        calls.append(request)
        return model_response({"analysis": "局部帧仍需要看原定片段", "need_clips": True})

    async def scenario():
        async with paid_media(core, tmp_path, provider) as (_, item, prepared):
            async with real_core_http(core) as port:
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}", trust_env=False,
                    headers={"Authorization": "Bearer " + TOKEN},
                ) as client:
                    path = "/internal/v3/readers/bili/" + item["request_id"] + "/media"
                    first = await client.post(path, json=upload_body(item, prepared))
                    assert first.status_code == 200, first.text
                    assert first.json()["next_selection"] is not None
                    known = phase_costs(core)[0]
                    cap = 1_000_000 if ceiling == "item" else 20_000_000
                    consumed = (known["amount_micro_cny"] if ceiling == "item" else sum(
                        row["amount_micro_cny"] for row in core.store.rows(
                            "SELECT * FROM cloud_cost_snapshots")))
                    prior_usd = (cap - consumed) // 8
                    # Existing, settled native cost is the starting financial history.
                    with core.store.transaction():
                        cloud_budget.reserve(
                            core.store, core.settings, reference_type="model",
                            reference_id="settled-history", estimated_micro_usd=prior_usd,
                            task_kind="video" if ceiling == "item" else "model",
                            task_id=known["task_id"] if ceiling == "item" else "other-work",
                            now=core.clock(),
                        )
                        native(core.store, "settled-history", prior_usd, core.clock(),
                               state="SETTLED")
                        cloud_budget.sync_reference(core.store, "model", "settled-history")
                    before = phase_usage(core)
                    result = await client.post(path, json=upload_body(
                        item, prepared, first.json()["next_selection"],
                    ))
                    assert result.status_code == 429, result.text
                    assert result.json() == {"error": (
                        "CLOUD_TASK_BUDGET_EXHAUSTED" if ceiling == "item"
                        else "CLOUD_DAILY_BUDGET_EXHAUSTED"
                    )}
                    assert len(calls) == 1
                    assert phase_usage(core) == before
                    # The rejected clip never creates a new charge or new visual
                    # claim. Already settled preview evidence remains available.
                    retained = core.store.one("SELECT * FROM bili_video_items")
                    assert retained["state"] == "ANALYZED" and retained["output_event_id"]
                    observation = core.store.one("SELECT * FROM observations WHERE event_id=?",
                                                 (retained["output_event_id"],))
                    proof = json.loads(observation["evidence_json"])["bili_video"]
                    assert proof["partial"] and proof["complete_video"] is False
                    assert proof["audio_analyzed"] is False
                    assert [phase["phase"] for phase in proof["phases"]] == ["preview"]
                    assert core.store.one("SELECT state FROM bili_video_phases "
                                          "WHERE phase='clips'") == {"state": "REJECTED"}
                    assert not core.store.one("SELECT 1 FROM outbox")

    run(scenario())
