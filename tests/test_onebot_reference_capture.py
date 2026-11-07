"""References are fetched after owner authentication and frozen into capture evidence."""

import asyncio
import json

import httpx
import pytest
from websockets.asyncio.server import serve

from companion_onebot_bridge.app import receive
from companion_onebot_bridge.metrics import Metrics
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.live_image_input import options
from tests.support.native_images import image_host
from tests.support.onebot_events import config, raw_event


@pytest.mark.parametrize("corrupt", [False, True])
def test_capture_binds_fetched_body_and_approval_never_refetches(core, tmp_path, corrupt):
    calls = []
    raw = raw_event(message=[
        {"type": "text", "data": {"text": "解释引用并总结转发"}},
        {"type": "reply", "data": {"id": "43"}},
        {"type": "forward", "data": {"id": "first-forward"}},
    ])

    async def source(ws):
        raw["time"] = int(core.clock.now.timestamp())
        await ws.send(json.dumps(raw))
        await ws.wait_closed()

    def protocol(request):
        if request.url.path not in {"/get_msg", "/get_forward_msg"}:
            return None
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path == "/get_msg":
            assert body == {"message_id": 43}
            data = {"message_id": 43, "sender": {"nickname": "引用作者"}, "message": [
                {"type": "text", "data": {"text": "这是引用原文"}},
                {"type": "image", "data": {"url": "https://gchat.qpic.cn/private-refresh?key=x"}},
            ]}
        elif body == {"id": "first-forward"}:
            data = {"messages": [
                {"sender": {"nickname": "甲"}, "content": [
                    {"type": "text", "data": {"text": "第一条正文"}},
                    {"type": "forward", "data": {"id": "nested-forward"}},
                ]},
                {"sender": {"nickname": "丙"}, "content": "第三条正文"},
            ]}
        else:
            assert body == {"id": "nested-forward"}
            data = {"messages": [{"sender": {"nickname": "乙"}, "content": "第二条正文"}]}
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})

    async def scenario():
        async with image_host(core, tmp_path, source, options=options(), protocol_reply=protocol,
        ) as (active, _, sends, model_calls):
            await active.capture()
            row = active.db.execute("SELECT body FROM inbox").fetchone()
            event = json.loads(row[0])
            segments = event["payload"]["rich"]["segments"]
            assert segments[0]["body"]["nodes"][0]["text"] == "这是引用原文"
            assert [(node["nickname"], node["text"]) for node in segments[1]["body"]["nodes"]] == [
                ("甲", "第一条正文"), ("乙", "第二条正文"), ("丙", "第三条正文")]
            assert len(calls) == 3 and "private-refresh" not in row[0]
            assert "private-refresh" not in active.runtime.path.read_text("utf-8")
            if corrupt:
                event["payload"]["rich"]["segments"][0]["body"]["nodes"][0]["text"] = "伪造"
                with active.db:
                    active.db.execute("UPDATE inbox SET body=?", (json.dumps(event),))
                with pytest.raises(ValueError, match="OWNER_CAPTURE_PROJECTION_CHANGED"):
                    await active.flush()
                assert not core.store.one("SELECT 1 FROM events")
            else:
                await active.flush()
                saved = core.store.one("SELECT body_json FROM events WHERE source='qq.onebot'")
                assert json.loads(saved["body_json"])["rich"] == event["payload"]["rich"]
                assert active.db.execute("SELECT body FROM inbox").fetchone()[0] is None
                active.runtime.verify()
            assert len(calls) == 3 and sends == model_calls == []

    run(scenario())


def test_daemon_reads_only_new_authenticated_owner_reference(tmp_path):
    class Reader:
        calls = 0

        async def get_msg(self, reference):
            assert reference == "43"
            self.calls += 1
            return {"message_id": 43, "message": "只读取一次"}

    async def scenario():
        stop, wake, complete = asyncio.Event(), asyncio.Event(), asyncio.Event()
        reader = Reader()
        raw = raw_event(message=[{"type": "reply", "data": {"id": "43"}}])

        async def source(ws):
            await ws.send(json.dumps(raw | {"user_id": 99999, "sender": {"user_id": 99999}}))
            await ws.send(json.dumps(raw))
            await ws.send(json.dumps(raw))
            await ws.wait_closed()

        def record(row):
            if row["stage"] == "duplicate_suppressed":
                complete.set()

        async with serve(source, "127.0.0.1", 0) as server:
            cfg = config(data_dir=str(tmp_path / "bridge"), rich_input_scope="references_v1",
                         onebot_ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                         onebot_http_url="http://127.0.0.1:3000")
            with Spool(cfg) as spool:
                task = asyncio.create_task(receive(
                    cfg, spool, wake, stop, Metrics("OFFLINE_FIXTURE", record), reader,
                ))
                try:
                    await asyncio.wait_for(complete.wait(), 5)
                finally:
                    stop.set()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                assert spool.pending_count == 1 and reader.calls == 1
                assert "只读取一次" in spool.db.execute("SELECT body FROM inbox").fetchone()[0]

    run(scenario())
