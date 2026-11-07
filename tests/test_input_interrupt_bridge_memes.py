"""Catalog offers and final image dispatch through real API and independent workers."""

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import pytest

from companion.core import Core
from companion.model import ModelClient
from companion_onebot_bridge import app, owner_notices
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.owner_memes import MemePump
from companion_onebot_bridge.profile_probe import ProfilePump
from tests.conftest import run
from tests.native_continuous_support import frame, host, recall_frame, until
from tests.support.input_interrupt_bridge import model_response
from tests.support.private_companion import companion_options
from tests.test_meme_runtime import RULE
from tests.test_next_memes_catalog import metadata, png


def options(core, *, discovery=False):
    value = companion_options(core)
    value["core_options"].update(expression_selection_probability=1,
                                meme_discovery_enabled=discovery,
                                meme_discovery_daily_download_bytes=32 * 1024 * 1024,
                                meme_discovery_daily_storage_bytes=8 * 1024 * 1024)
    return value


@asynccontextmanager
async def meme_host(core, tmp_path, source, *, discovery=False, **kwargs):
    async with host(core, tmp_path, source,
        purposes=("chat", "meme", "expression"),
        core_options=options(core, discovery=discovery)["core_options"],
        bridge_options={"meme_enabled": True, "meme_discovery_enabled": discovery,
                        "meme_trusted_sources": (RULE,)}, **kwargs) as values:
        yield values


@pytest.mark.parametrize("outcome", ["sent", "missing", "unknown"])
def test_selected_catalog_bytes_use_real_api_independent_worker_and_receipts(
    core, tmp_path, monkeypatch, outcome,
):
    from companion import api

    calls, invoked, instances = [], [], []
    runtime = candidate = None

    async def source(ws):
        await ws.send(frame(1, text="太棒了，成功了"))
        await ws.wait_closed()

    def construct(*args, **kwargs):
        instance = Core(*args, clock=core.clock, **kwargs)
        instances.append(instance)
        return instance

    def answer(request):
        calls.append(json.loads(request.content)["messages"])
        if outcome == "missing":
            runtime.catalog.remove(candidate)
        return model_response("完成啦！⬡" + candidate + "⬡")

    def protocol_reply(request):
        if request.url.path == "/send_private_msg":
            invoked.append(request)
            if outcome == "unknown":
                raise httpx.ReadTimeout("synthetic delivery response loss")

    async def scenario():
        nonlocal runtime, candidate
        monkeypatch.setattr(api, "Core", construct)
        async with meme_host(core, tmp_path, source, independent_api=True,
            protocol_reply=protocol_reply) as (window, _, sends, _), httpx.AsyncClient(
            transport=httpx.MockTransport(answer),
        ) as model_http:
            runtime = window.runtime.memes
            candidate = runtime.catalog.add(png(), metadata(), check=window.runtime.verify)
            await window.capture()
            await window.flush()
            with Core(core.settings, model=ModelClient(core.settings, model_http),
                      clock=core.clock) as independent:
                assert await independent.run_once()
                assert instances[0] is not independent
                assert instances[0].store is not independent.store
            assert await window.pump()
            assert len(calls) == len(invoked) == 1
            assert candidate in json.dumps(calls[0], ensure_ascii=False)
            pin = runtime.db.execute("SELECT state,sent_at FROM meme_pins").fetchone()
            if outcome == "unknown":
                assert window.runtime.row()["state"] == "STOPPED" and pin[0] == "UNKNOWN"
                assert sends == [] and pin[1] is None
                with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                    window.runtime.verify()
                before = core.store.rows("SELECT * FROM model_usage")
                deliveries = core.store.rows("SELECT * FROM outbox")
                with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                    await window.pump()
                assert len(invoked) == 1
                assert core.store.rows("SELECT * FROM model_usage") == before
                assert core.store.rows("SELECT * FROM outbox") == deliveries
                assert deliveries[0]["state"] == "DELIVERY_UNCERTAIN"
            else:
                assert window.runtime.row()["state"] == "ACTIVE" and len(sends) == 1
                parts = sends[0]["message"]
                assert parts[0] == {"type": "text", "data": {"text": "完成啦！"}}
                if outcome == "sent":
                    assert len(parts) == 2 and pin[0] == "SENT" and pin[1] is not None
                    assert parts[1]["data"]["file"] == "base64://" + base64.b64encode(
                        png()).decode("ascii")
                else:
                    assert len(parts) == 1 and pin[0] == "CANCELLED"
            assert core.store.one("SELECT COUNT(*) n FROM model_usage WHERE state='SETTLED'")[
                "n"] == 1
    run(scenario())


@pytest.mark.parametrize("control", ["recall", "new_input", "duplicate"])
def test_received_control_fences_prepared_meme_before_dispatch(core, tmp_path, control):
    invoked = []
    number = 0
    original = None

    async def source(ws):
        nonlocal number, original
        number += 1
        if number == 1:
            original = frame(1, text="太棒了，成功了")
        await ws.send(original if number == 1 or control == "duplicate"
                      else frame(2, text="换个话题"))
        await ws.wait_closed()

    def remote(request):
        if request.url.path == "/send_private_msg":
            invoked.append(request)

    async def scenario():
        async with meme_host(core, tmp_path, source, protocol_reply=remote) as (
            window, _, sends, _,
        ):
            runtime = window.runtime.memes
            candidate = runtime.catalog.add(png(), metadata(), check=window.runtime.verify)
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: model_response("完成啦！⬡" + candidate + "⬡"),
            )) as model_http:
                core.model = ModelClient(core.settings, model_http)
                await window.capture()
                await window.flush()
                assert await core.run_once()

                async def before(request):
                    if request.url.path == "/send_private_msg":
                        assert runtime.db.execute("SELECT state FROM meme_pins").fetchone()[0] == (
                            "PREPARED")
                        if control == "recall":
                            # Match the Core and Bridge clock, including between ticker runs.
                            received_at = core.clock.now
                            owner_notices.put(window.runtime,
                                recall_frame(time=int(received_at.timestamp())), received_at,
                                ("127.0.0.1", 12345))
                        elif control == "new_input":
                            await window.capture(2)
                        else:
                            # Replay the exact previously authenticated frame and original receipt.
                            capture = json.loads(window.db.execute(
                                "SELECT capture_json FROM window_inputs").fetchone()[0])
                            assert not window.runtime.persist_capture(
                                normalize(original, window.config), original,
                                received_at=datetime.fromisoformat(capture["received_at"]),
                                peer=("127.0.0.1", 12345))

                window.protocol.event_hooks["request"].append(before)
                assert await window.pump()
                assert window.runtime.row()["state"] == "ACTIVE"
                if control == "duplicate":
                    assert window.attempts() == {"ACK": 1}
                    assert len(sends) == len(invoked) == 1
                    assert runtime.db.execute("SELECT state FROM meme_pins").fetchone()[0] == "SENT"
                    assert window.spool.pending_count == 0
                else:
                    assert window.attempts() == {"NOT_SENT": 1}
                    assert sends == invoked == []
                    assert runtime.db.execute("SELECT state FROM meme_pins").fetchone()[0] == (
                        "CANCELLED")
                    assert core.store.one("SELECT state FROM outbox")["state"] == "CANCELLED"
                    if control == "new_input":
                        assert core.store.one("SELECT reply_revision FROM conversations")[
                            "reply_revision"] == 1
                        assert window.spool.pending_count == 1
    run(scenario())


def test_idle_discovery_yields_to_new_input_without_stopping_chat(core, tmp_path, monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()
    public_calls, model_calls = [], []

    async def source(ws):
        await ws.send(frame(1, text="太棒了，成功了"))
        await asyncio.wait_for(started.wait(), 20)
        await ws.send(frame(2, text="换个话题，聊聊阅读"))
        await ws.wait_closed()

    async def public(request):
        public_calls.append(request)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def resolver(*_):
        return ["93.184.216.34"]

    def answer(request):
        model_calls.append(request)
        return model_response("好，我们聊聊。")

    async def scenario():
        async with meme_host(core, tmp_path, source, discovery=True, model_reply=answer) as (
            window, _, sends, _,
        ):
            window.runtime.memes.transport = httpx.MockTransport(public)
            window.runtime.memes.resolver = resolver
            pump = MemePump(window.runtime, window.client)
            profile = ProfilePump(window.runtime, window.client, window.protocol)
            window.start()
            task = asyncio.create_task(app.discover_memes(pump, window.stop, profile))
            try:
                await until(lambda: cancelled.is_set() and window.attempts() == {"ACK": 2},
                    tasks=[task, *window.tasks], deadline_seconds=20)
                assert window.runtime.row()["state"] == "ACTIVE"
                assert len(sends) == 2 and len(public_calls) == 1 and len(model_calls) == 2
                assert core.store.one("SELECT state FROM meme_discovery_reservations")[
                    "state"] == "UNKNOWN"
                assert core.store.one("SELECT COUNT(*) n FROM model_usage WHERE state='SETTLED'")[
                    "n"] == 2
            finally:
                started.set()
                await window.pause()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await pump.close()
    run(scenario())


def test_capture_replays_frozen_offer_after_lost_core_approval_ack(core, tmp_path):
    requests = []
    number = 0

    async def source(ws):
        nonlocal number
        number += 1
        await ws.send(frame(number, text="太棒了，成功了" if number == 1 else "换个话题"))
        await ws.wait_closed()

    async def scenario():
        async with meme_host(core, tmp_path, source) as (window, _, sends, _):
            runtime = window.runtime.memes
            original = runtime.catalog.add(png(), metadata(), check=window.runtime.verify)
            await window.capture()

            async def lost_ack(response):
                if response.request.url.path.endswith("/inputs/approve"):
                    assert response.status_code == 200
                    requests.append(json.loads(response.request.content))
                    if len(requests) == 1:
                        raise httpx.ReadTimeout("synthetic accepted approval ACK lost")

            window.client.event_hooks["response"].append(lost_ack)
            with pytest.raises(httpx.ReadTimeout):
                await window.runtime.flush_one(window.client)
            assert window.spool.pending_count == 1
            runtime.catalog.remove(original)
            later = runtime.catalog.add(png("red"), metadata(), check=window.runtime.verify)
            await window.capture(2)
            assert await window.runtime.flush_one(window.client)
            assert window.runtime.row()["state"] == "ACTIVE" and sends == []
            assert len(requests) == 2 and requests[0] == requests[1]
            candidates = requests[1]["meme_offer"]["candidates"]
            assert [item["candidate_id"] for item in candidates] == [original]
            assert later not in {item["candidate_id"] for item in candidates}
            assert window.spool.pending_count == 1
            assert [row[0] for row in window.db.execute(
                "SELECT state FROM inbox ORDER BY rowid")] == ["accepted", "pending"]
    run(scenario())
