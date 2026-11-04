"""An ACK interval yields to the next private segment, rather than account polling."""

import asyncio
import json

from companion.config import SegmentedReplyConfig
from companion_onebot_bridge import app
from tests.conftest import run
from tests.native_continuous_support import frame, host, until
from tests.sdk_reader_fixture import Provider, answer, worker_for


def test_segment_interval_does_not_enter_slow_account_poll(core, tmp_path, monkeypatch):
    async def source(ws):
        await ws.send(frame())
        await ws.wait_closed()

    async def scenario():
        policy = SegmentedReplyConfig(max_segments="unlimited", mode="semantic", interval_ms=600)
        async with host(core, tmp_path, source, policy=policy, ticking=False) as (
            active, _, sends, _,
        ):
            await active.capture()
            await active.flush()
            model = Provider(answer("第一段合成回复\n第二段合成回复"))
            async with worker_for(core, model) as worker:
                assert await worker.run_once("segment-account-priority")
            usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
            assert len(model.requests) == len(usage) == 1 and usage[0]["state"] == "SETTLED"
            assert await active.pump() and len(sends) == 1
            core.clock.advance(0.59)
            stop, paused, blocked, release = (asyncio.Event() for _ in range(4))
            requests, intervals = [], []
            original_pause = app.pause

            async def observe(request):
                path = request.url.path
                body = json.loads(request.content)
                if path.endswith("/deliveries/lease"):
                    requests.append(("lease", body.get("delivery_lane", "owner_private")))
                if path.endswith("/owner-authority/check") and not requests:
                    requests.append(("fallback_permission", body["purpose"]))
                    blocked.set()
                    await release.wait()

            async def pause(event, seconds):
                if len(sends) == 1:
                    intervals.append(seconds)
                    paused.set()
                await original_pause(event, seconds)

            monkeypatch.setattr(app, "pause", pause)
            active.client.event_hooks["request"].append(observe)
            task = asyncio.create_task(app.deliver(active.client, active.onebot, active.spool,
                stop, active.metrics, runtime=active.runtime, protocol=active.protocol))
            try:
                await until(lambda: paused.is_set() or blocked.is_set(), tasks=[task])
                assert not blocked.is_set(), {"requests": requests, "sends": len(sends)}
                assert intervals and len(sends) == 1 and active.attempts() == {"ACK": 1}
                assert intervals[0] == 0.01
                core.clock.advance(0.01)
                await until(lambda: active.attempts() == {"ACK": 2}, tasks=[task],
                            deadline_seconds=20)
                assert len(sends) == 2
                assert requests[0] == ("lease", "owner_private")
                assert core.store.rows("SELECT state FROM outbox ORDER BY segment_index") == [
                    {"state": "SENT"}, {"state": "SENT"}]
                assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
            finally:
                stop.set()
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    run(scenario())
