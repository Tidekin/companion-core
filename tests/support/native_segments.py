"""Native owner ingress and real checked OneBot delivery for segmented replies."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx

from companion import owner_authority
from companion.model import ModelClient
from companion.reply_segments import SegmentedReplyConfig
from tests.support.native_context import source_window
from tests.support.onebot_events import raw_event


@asynccontextmanager
async def session(core, tmp_path, *, reply="第一段合成回答。第二段合成回答。", cap=3):
    number, calls = 0, []

    async def source(ws):
        await ws.send(json.dumps(raw_event(number, time=int(core.clock.now.timestamp()),
                                            text=f"这是第{number}轮合成输入。")))
        await ws.wait_closed()

    options = {"max_history_utf8_bytes": 4000, "owner_policy": {"max_reply_chars": 500},
               "core_options": {"segmented_reply": SegmentedReplyConfig(
                   max_segments=cap, min_chars=4, interval_ms=500), "max_recent_messages": 4}}
    async with source_window(core, tmp_path, source, scope_options=options) as values:
        captured, client, protocol, _, sends = values

        def respond(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": reply}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

        async def admit():
            nonlocal number
            number += 1
            await captured.capture_once(client, protocol)
            assert await captured.runtime.flush_one(client)
            return core.store.one(
                'SELECT j.*,e.conversation_id FROM jobs j JOIN events e '
                'USING(event_id) ORDER BY j.record_sequence DESC LIMIT 1'
            )

        async def send():
            assert (await captured.send(client, protocol))["worked"]

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as model:
            core.model = ModelClient(core.settings, model)
            yield SimpleNamespace(admit=admit, send=send, calls=calls, sends=sends,
                owner=captured.runtime, client=client,
                view=owner_authority.current(core.store, core.settings, core.clock))
