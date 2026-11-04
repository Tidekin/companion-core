"""Real child death preserves native upload custody without re-downloading originals."""

import asyncio
import hashlib
import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from companion import __main__, image_spool
from companion.contracts import CoreError
from companion_onebot_bridge.pending_media import MediaPump
from tests.conftest import run
from tests.native_continuous_support import host, until
from tests.support.onebot_events import raw_event
from tests.test_live_image_input import options, png
from tests.test_onebot_images import image_segment

ROOT = Path(__file__).resolve().parents[1]

CHILD = """
import asyncio,json,os,sys
from datetime import datetime
from pathlib import Path
import httpx
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.pending_media import MediaPump
from companion_onebot_bridge.spool import Spool
value=json.loads(Path(sys.argv[1]).read_text('utf8'))
cfg=BridgeConfig(**value['config'])
log=Path(value['log'])
def record(kind, **fields):
    with log.open('a',encoding='utf8') as stream:
        stream.write(json.dumps({'kind':kind,**fields})+'\\n')
        stream.flush()
        os.fsync(stream.fileno())
async def resolve(*args):
    return ['1.1.1.1']
def download(request):
    record('download')
    return httpx.Response(200,content=Path(value['image']).read_bytes(),
                          headers={'Content-Type':'image/png'})
async def before(request):
    if request.url.path.endswith('/image') and request.method=='POST':
        record('upload',proof=json.loads(request.headers['X-Content-Proof']))
        if value['phase']=='before_staging':
            os._exit(17)
async def after(response):
    if response.request.url.path.endswith('/image') and response.request.method=='POST':
        await response.aread()
        assert response.status_code==200,response.text
        record('staged',receipt=response.json())
        os._exit(17)
async def main():
    with Spool(cfg) as spool:
        runtime=OwnerRuntime(spool,clock=lambda:datetime.fromisoformat(value['now']))
        async with private_http_client(cfg.core_url,cfg.bridge_token) as client:
            client.event_hooks['request'].append(before)
            client.event_hooks['response'].append(after)
            pump=MediaPump(runtime,client,image_resolver=resolve,
                           image_transport=httpx.MockTransport(download))
            pump.tick()
            assert pump.task is not None
            await pump.task
    raise AssertionError('crash boundary was not reached')
asyncio.run(main())
"""


@pytest.mark.parametrize("phase", ["before_staging", "after_staging"])
def test_child_exit_before_reserve_keeps_one_shot_content_custody(
    core, tmp_path, monkeypatch, phase,
):
    async def source(ws):
        await ws.send(json.dumps(raw_event(time=int(core.clock.now.timestamp()),
                                          message=[image_segment()])))
        await ws.wait_closed()

    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "看到了蓝色。"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    monkeypatch.setattr(__main__, "httpx", SimpleNamespace(AsyncClient=lambda **kwargs:
        httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(provider))))

    async def actual():
        scope = options()
        async with host(core, tmp_path, source, ticking=False,
            core_options={**scope["core_options"], "rich_input_scope": "references_v1"},
            paid_options=scope["paid_options"], purposes=("chat", "image", "content"),
            bridge_options={"rich_input_scope": "references_v1"}, model_reply=provider,
        ) as (active, _, sends, _):
            core.content.options = core.settings.content
            await active.capture()
            await active.flush()
            assert active.db.execute("SELECT state FROM automatic_media").fetchone()[0] == "PENDING"
            image = tmp_path / "original.png"
            image.write_bytes(png())
            log = tmp_path / "child-receipts.jsonl"
            config = tmp_path / "child-input.json"
            config.write_text(json.dumps({"config": asdict(active.config), "now": core.clock(),
                "phase": phase, "image": str(image), "log": str(log)}), encoding="utf8")
            active.spool.close()
            child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c", CHILD,
                str(config), cwd=ROOT,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                async with asyncio.timeout(40):
                    _, error = await child.communicate()
                assert child.returncode == 17, error.decode(errors="replace")
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
                active.restart()
            records = [json.loads(line) for line in log.read_text("utf8").splitlines()]
            assert [row["kind"] for row in records] == ["download", "upload", *(
                ["staged"] if phase == "after_staging" else [])]
            assert active.db.execute(
                "SELECT state FROM automatic_media").fetchone()[0] == "UPLOADING"
            item = core.store.one("SELECT * FROM content_items")
            assert item["state"] == ("FETCHING" if phase == "before_staging" else "SUCCEEDED")
            assert item["lease_token"] == records[1]["proof"]["lease_token"]
            assert calls == [] and not core.store.rows("SELECT * FROM model_admissions")
            original_receipt = core.store.rows("SELECT * FROM captured_images")
            original_spool = (image_spool.get(core.store, item, core.clock(), 50000)
                              if original_receipt else None)

            def never_download(request):
                pytest.fail("An uncertain upload cannot re-download")

            recovery = MediaPump(active.runtime, active.client,
                                 image_transport=httpx.MockTransport(never_download))
            try:
                if phase == "before_staging":
                    core.clock.now = (datetime.fromisoformat(item["lease_until"])
                                      + timedelta(seconds=1))
                    await core.content.run_once()
                    assert core.store.one("SELECT state FROM content_items")["state"] == "FAILED"
                    recovery.tick()
                    await recovery.task
                    assert active.db.execute(
                        "SELECT state FROM automatic_media").fetchone()[0] == "DONE"
                    with pytest.raises(CoreError, match="CONTENT_IMAGE_ALREADY_FAILED"):
                        core.content.claim_upload(item["owner_event_id"], "image:0",
                                                  records[1]["proof"]["source_sha256"])
                    assert core.store.rows("SELECT * FROM captured_images") == []
                else:
                    recovery.tick()
                    await recovery.task
                    assert active.db.execute(
                        "SELECT state FROM automatic_media").fetchone()[0] == "DONE"
                    assert records[-1]["receipt"] == {
                        "state": "SUCCEEDED", "slot": 0, "duplicate": False, "input_ready": True}
                    assert core.store.rows("SELECT * FROM captured_images") == original_receipt
                    assert image_spool.get(core.store, item, core.clock(), 50000) == original_spool
                    assert item["media_sha256"] == hashlib.sha256(png()).hexdigest()
                    task = asyncio.create_task(__main__.worker(core.settings))
                    try:
                        await until(lambda: core.store.one("SELECT 1 FROM outbox"),
                                    tasks=[task], deadline_seconds=20)
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    assert len(calls) == 1
                    assert core.store.rows("SELECT scenario,state FROM model_usage") == [
                        {"scenario": "chat", "state": "SETTLED"}]
                assert log.read_text("utf8").splitlines() == [json.dumps(row) for row in records]
                assert sends == []
                active.runtime.verify()
            finally:
                await recovery.close()
    run(actual())
