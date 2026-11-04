"""Real native capture, SDK and checked transport fixtures for dispatch fences."""

import json
from contextlib import asynccontextmanager

import httpx

from companion.model import ModelClient
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from tests.support.native_context import source_window
from tests.support.onebot_events import raw_event


@asynccontextmanager
async def staged(core, tmp_path):
    async def source(ws):
        await ws.send(json.dumps(raw_event()))
        await ws.wait_closed()

    async with source_window(core, tmp_path, source) as (capture, client, protocol, _, sends):
        await capture.capture_once(client, protocol)
        await capture.runtime.flush_one(client)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, json={"choices": [{"message": {"content": "合成回答"}}],
                                     "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
        )) as model:
            core.model = ModelClient(core.settings, model)
            assert await core.run_once("native-dispatch")
        guard = OwnerSendGuard(capture.runtime, client)
        yield capture.runtime, client, protocol, guard, sends


async def dispatch(runtime, client, protocol, guard):
    return await deliver_tick(client, OwnerOneBot(guard, protocol), runtime.spool, guard=guard)


def attempts(runtime):
    return dict(runtime.db.execute("SELECT state,count(*) FROM window_attempts GROUP BY state"))
