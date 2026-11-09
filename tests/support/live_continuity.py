import json
from contextlib import asynccontextmanager

import httpx

from tests.native_continuous_support import frame, host


@asynccontextmanager
async def native_continuity(core, tmp_path, *, turns=4, byte_limit=16000, texts=None,
                            soft_limit=3200, core_options=None, paid_options=None,
                            grant_options=None, respond=None, purposes=None):
    texts = texts or ["选了茉莉绿茶", "现在说的这个呢？", "刚才聊到哪了？", "还是那个吗？", "继续"]
    number, captures = 0, 0
    calls = []

    async def source(ws):
        nonlocal number
        number += 1
        # Wire event time and app.receive use the wall clock; the independently
        # advancing Core clock belongs to business deadlines and history tests.
        await ws.send(frame(number, text=texts[number - 1]))
        await ws.wait_closed()

    def provider_reply(request):
        calls.append(json.loads(request.content))
        if respond:
            return respond(request)
        return httpx.Response(200, json={"choices": [{"message": {
            "content": f"合成回答{len(calls)}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    from datetime import UTC, datetime, timedelta

    selected = purposes or (["chat", "history"] if turns else ["chat"])
    history = {"scope": "owner_private", "not_before": datetime.now(UTC) - timedelta(days=1),
               "max_turns": turns, "max_utf8_bytes": byte_limit} if turns else {}
    async with host(core, tmp_path, source, model_reply=provider_reply,
        purposes=selected, core_options={"max_recent_messages": turns * 2,
            "context_soft_tokens": soft_limit, **(core_options or {})},
        paid_options={"daily_calls": 100, "window_calls": 100, **(paid_options or {})},
        grant_options={"history": history, **(grant_options or {})},
    ) as (active, _, sends, _):
        async def capture_input():
            nonlocal captures
            captures += 1
            await active.capture(captures)
            # app.receive stamps received_at with the wall clock, and owner history
            # rejects a source received after Core's now. The ticking Core clock may
            # trail that wall clock (Windows CI measured about 0.9s), so never let
            # it lag a receipt it is about to admit; a clock set ahead is kept.
            core.clock.now = max(core.clock.now, datetime.now(UTC))
            await active.flush()

        async def finish():
            assert await core.run_once("native-continuity")
            return await active.pump()

        yield active, active.client, calls, sends, capture_input, finish


def history_snapshot(core):
    row = core.store.one(
        'SELECT snapshots_json FROM agent_conversations ORDER BY record_sequence DESC LIMIT 1')
    return next(item["proof"] for item in json.loads(row["snapshots_json"])
                if item["kind"] == "history")
