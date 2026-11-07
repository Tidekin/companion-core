"""Authenticated receive priority across independent API and chat worker instances."""

import asyncio
import json

import httpx

from companion.core import Core
from companion.model import ModelClient
from companion.reply_segments import SegmentedReplyConfig
from tests.conftest import run
from tests.native_continuous_support import frame, host, until, worker
from tests.support.input_interrupt_bridge import model_response


def test_slow_reply_does_not_block_accepting_new_input_with_independent_api_worker(
    core, tmp_path, monkeypatch,
):
    from companion import api

    started, release, new_accepted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls, instances = [], []

    async def source(ws):
        await ws.send(frame(1, text="先说第一件事"))
        await asyncio.wait_for(started.wait(), 15)
        second = frame(2, text="改聊第二件事")
        await ws.send(second)
        await ws.send(second)
        await ws.wait_closed()

    def construct(*args, **kwargs):
        instance = Core(*args, clock=core.clock, **kwargs)
        instances.append(instance)
        return instance

    async def answer(request):
        calls.append(json.loads(request.content)["messages"])
        if len(calls) == 1:
            started.set()
            await asyncio.wait_for(release.wait(), 15)
            return model_response("过期的第一条回复")
        return model_response("第二件事的新回复")

    async def scenario():
        monkeypatch.setattr(api, "Core", construct)
        async with host(core, tmp_path, source, model_reply=answer, independent_api=True) as (
            window, _, sends, _,
        ), httpx.AsyncClient(transport=httpx.MockTransport(answer)) as model_http:
            independent = Core(core.settings, model=ModelClient(core.settings, model_http),
                               clock=core.clock)
            stop = asyncio.Event()

            async def received(response):
                if response.request.url.path == "/internal/v1/inbox/events" and (
                    json.loads(response.request.content)["payload"]["text"] == "改聊第二件事"
                    and response.status_code == 202
                ):
                    new_accepted.set()

            window.client.event_hooks["response"].append(received)
            task = asyncio.create_task(worker(independent, stop))
            window.start(worker=False)
            try:
                await asyncio.wait_for(new_accepted.wait(), 15)
                assert core.store.one("SELECT COUNT(*) n FROM events")["n"] == 2
                assert started.is_set() and not release.is_set() and not sends
                release.set()
                await until(lambda: window.attempts() == {"ACK": 1},
                            tasks=[task, *window.tasks], deadline_seconds=15)
                assert window.runtime.row()["state"] == "ACTIVE" and window.captures() == 2
                assert [item["message"][0]["data"]["text"] for item in sends] == [
                    "第二件事的新回复"]
                jobs = core.store.rows("SELECT state FROM jobs WHERE kind='chat' ORDER BY rowid")
                assert [item["state"] for item in jobs] == ["CANCELLED", "SUCCEEDED"]
                assert core.store.one("SELECT reply_revision FROM conversations")[
                    "reply_revision"] == 2
                assert len(calls) == 2
                assert core.store.one("SELECT COUNT(*) n FROM model_usage "
                                      "WHERE state='SETTLED'")["n"] == 2
                assert instances[0] is not independent
                assert instances[0].store is not independent.store
            finally:
                release.set()
                started.set()
                stop.set()
                await window.pause()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                independent.close()
    run(scenario())


def test_new_input_between_bubbles_keeps_ack_prefix_and_cancels_old_tail(
    core, tmp_path, monkeypatch,
):
    first_sent = asyncio.Event()
    calls = []

    async def source(ws):
        await ws.send(frame(1, text="先详细说第一件事"))
        await asyncio.wait_for(first_sent.wait(), 15)
        await ws.send(frame(2, text="等一下，先说第二件事"))
        await ws.wait_closed()

    def answer(request):
        calls.append(json.loads(request.content)["messages"])
        return model_response("第一段\n第二段\n第三段" if len(calls) == 1 else "新的话题")

    def sent(request):
        if request.url.path == "/send_private_msg":
            first_sent.set()

    async def scenario():
        policy = SegmentedReplyConfig(mode="semantic", interval_ms=1500)
        async with host(core, tmp_path, source, policy=policy, model_reply=answer,
                        protocol_reply=sent) as (window, _, sends, _):
            window.start()
            try:
                await until(lambda: window.attempts() == {"ACK": 2},
                            tasks=window.tasks, deadline_seconds=15)
                assert window.runtime.row()["state"] == "ACTIVE"
                assert [item["message"][0]["data"]["text"] for item in sends] == [
                    "第一段", "新的话题"]
                original = core.store.rows("SELECT state FROM outbox WHERE origin_event_id="
                    "(SELECT event_id FROM jobs WHERE kind='chat' ORDER BY rowid LIMIT 1) "
                    "ORDER BY segment_index")
                assert [row["state"] for row in original] == ["SENT", "CANCELLED", "CANCELLED"]
                assert len(calls) == 2
                assert core.store.one("SELECT reply_revision FROM conversations")[
                    "reply_revision"] == 2
            finally:
                first_sent.set()
                await window.pause()
    run(scenario())


def test_buffered_owner_turn_fences_plain_reply_before_core_ingests_it(core, tmp_path):
    number = 0

    async def source(ws):
        nonlocal number
        number += 1
        await ws.send(frame(number, text="先谈第一件事" if number == 1 else "等等，换个话题"))
        await ws.wait_closed()

    async def scenario():
        async with host(core, tmp_path, source,
                        model_reply=lambda request: model_response("旧话题回复")) as (
            window, _, sends, _,
        ):
            await window.capture()
            await window.flush()
            assert await core.run_once()

            async def before(request):
                if request.url.path == "/send_private_msg":
                    await window.capture(2)

            window.protocol.event_hooks["request"].append(before)
            assert await window.pump()
            assert window.runtime.row()["state"] == "ACTIVE"
            assert window.attempts() == {"NOT_SENT": 1}
            assert sends == [] and window.spool.pending_count == 1
            assert core.store.one("SELECT reply_revision FROM conversations")["reply_revision"] == 1
            assert core.store.one("SELECT state FROM outbox")["state"] == "CANCELLED"
            proof = json.loads(window.db.execute("SELECT body FROM results").fetchone()[0])
            assert proof["result"] == "NOT_SENT"
            window.runtime.verify(active=False)
    run(scenario())
