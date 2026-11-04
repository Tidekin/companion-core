import asyncio
import json

from websockets.asyncio.server import serve

from companion_onebot_bridge.app import receive, upload
from companion_onebot_bridge.metrics import Metrics
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.onebot_events import config, raw_event
from tests.test_onebot_delivery import core_client


def test_real_websocket_reconnect_and_real_core_http_keep_one_reply(core, tmp_path):
    async def scenario():
        stop, wake = asyncio.Event(), asyncio.Event()
        complete = asyncio.Event()
        connections = 0

        async def source(ws):
            nonlocal connections
            assert ws.request.headers["Authorization"] == "Bearer " + cfg.onebot_token
            connections += 1
            await ws.send(json.dumps(raw_event(1)))
            await ws.close()

        records = []

        def record(row):
            records.append(row)
            if {r["stage"] for r in records} >= {"duplicate_suppressed", "t_core_ingest_ack"}:
                complete.set()

        metrics = Metrics("OFFLINE_FIXTURE", record)
        async with serve(source, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = config(
                data_dir=str(tmp_path / "bridge"),
                onebot_ws_url=f"ws://127.0.0.1:{port}",
                onebot_http_url="http://127.0.0.1:3000",
            )
            with Spool(cfg) as spool:
                async with core_client(core) as http:
                    tasks = [
                        asyncio.create_task(receive(cfg, spool, wake, stop, metrics)),
                        asyncio.create_task(upload(http, spool, wake, stop, metrics)),
                    ]
                    try:
                        async with asyncio.timeout(8):
                            await complete.wait()
                    finally:
                        stop.set()
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                assert spool.pending_count == 0
        assert await core.run_once("offline-ws")
        assert not await core.run_once("offline-ws")
        assert len(core.store.rows("SELECT * FROM events")) == 1
        assert len(core.store.rows("SELECT * FROM outbox")) == 1
        rendered = json.dumps(records)
        assert "你好" not in rendered and "10001" not in rendered
        assert "duplicate_suppressed" in rendered
        assert "t_core_ingest_ack" in rendered

    run(scenario())
