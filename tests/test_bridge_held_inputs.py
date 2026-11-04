"""Owner messages received while Core is unreachable are held, then admitted or refused once."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from websockets.asyncio.client import connect

from companion_onebot_bridge import held_inputs
from companion_onebot_bridge.backfill import backfill
from companion_onebot_bridge.capture import prepare_owner_input
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.onebot_client import OneBotClient
from tests.conftest import run
from tests.support import onebot
from tests.support.native_consumer_entry import environment
from tests.support.onebot_events import raw_event


async def receive(entry, number, text):
    """The live receive boundary with Core down: prepare fails, the frame is held."""
    frame = raw_event(number, text=text, time=int(entry.core.clock.now.timestamp()))
    entry.frames.put_nowait(json.dumps(frame))
    async with connect(entry.config.onebot_ws_url,
                       additional_headers={"Authorization": "Bearer " + entry.config.onebot_token},
                       proxy=None) as ws:
        received = await ws.recv()
        async with private_http_client("http://127.0.0.1:9", entry.config.bridge_token) as dead:
            with pytest.raises(httpx.TransportError) as error:
                await prepare_owner_input(entry.runtime, dead,
                                          OneBotClient(entry.config, entry.protocol), received,
                                          entry.core.clock.now, ws.remote_address)
        assert held_inputs.transient(error.value)
        assert held_inputs.hold(entry.runtime, received, entry.core.clock.now, ws.remote_address)


def results(entry):
    return [tuple(row) for row in entry.db.execute(
        "SELECT state,reason FROM held_inputs ORDER BY rowid")]


async def promote_all(entry):
    protocol = OneBotClient(entry.config, entry.protocol)
    while await held_inputs.promote_one(entry.runtime, entry.client, protocol):
        pass


def test_held_messages_are_admitted_in_order_with_their_original_receive_time(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path) as (entry, _):
            core.clock.now += timedelta(seconds=30)
            first_received = core.clock.now
            await receive(entry, 11, "API 重启时发的")
            core.clock.now += timedelta(seconds=1)
            await receive(entry, 12, "第二条")
            assert held_inputs.waiting(entry.runtime)
            # Nothing reached the inbox, the journal holds only hashes.
            assert entry.spool.pending_count == 0
            assert "API 重启时发的" not in entry.runtime.path.read_text(encoding="utf-8")
            core.clock.now += timedelta(seconds=40)
            await promote_all(entry)
            assert results(entry) == [("PROMOTED", None), ("PROMOTED", None)]
            assert entry.db.execute("SELECT COUNT(*) FROM held_inputs WHERE frame IS NOT NULL"
                                    ).fetchone()[0] == 0
            captures = [json.loads(row[0]) for row in entry.db.execute(
                "SELECT capture_json FROM window_inputs ORDER BY rowid")]
            assert [c["kind"] for c in captures] == ["authenticated_onebot_ws"] * 2
            assert captures[0]["received_at"] == first_received.isoformat()
            # Core applies its own checks at admission; both are accepted in order.
            assert await entry.runtime.flush_one(entry.client)
            assert await entry.runtime.flush_one(entry.client)
            # The second message supersedes the first one's reply, as for live input.
            assert [dict(row) for row in core.store.rows(
                "SELECT state,last_error_code FROM jobs ORDER BY rowid")] == [
                {"state": "CANCELLED", "last_error_code": "REPLY_SUPERSEDED"},
                {"state": "QUEUED", "last_error_code": None}]
            assert (await entry.state(captures[1]))["state"] == "PENDING"
            entry.runtime.verify()

    run(scenario())


def test_held_frames_expire_are_recalled_or_closed_by_stop(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path) as (entry, _):
            core.clock.now += timedelta(seconds=30)
            await receive(entry, 21, "太久")
            core.clock.now += timedelta(seconds=held_inputs.HOLD_MAX_SECONDS + 1)
            await receive(entry, 22, "被撤回")
            await entry.recall(22)
            core.clock.now += timedelta(seconds=1)
            await receive(entry, 23, "停止前")
            # The oldest expired; the recalled one is refused; nothing is admitted.
            protocol = OneBotClient(entry.config, entry.protocol)
            assert await held_inputs.promote_one(entry.runtime, entry.client, protocol)
            assert await held_inputs.promote_one(entry.runtime, entry.client, protocol)
            await entry.stop()
            assert results(entry) == [("REJECTED", "HOLD_EXPIRED"), ("REJECTED", "RECALLED"),
                                      ("REJECTED", "OWNER_STOPPED")]
            assert entry.spool.pending_count == 0
            entry.runtime.verify(active=False)

    run(scenario())


def test_backfill_waits_behind_held_frames_and_reports_coverage(core, tmp_path):
    async def scenario():
        onebot.HISTORY.clear()
        try:
            async with environment(core, tmp_path) as (entry, _):
                core.clock.now += timedelta(seconds=90)
                await receive(entry, 31, "先收到")
                now = core.clock.now.timestamp()
                frame = raw_event(32, text="补收", time=int(now))
                onebot.HISTORY.append({k: v for k, v in frame.items()
                                       if k not in {"post_type", "self_id"}})
                old = raw_event(30, text="太早", time=int(now) - 60)
                onebot.HISTORY.append({k: v for k, v in old.items()
                                       if k not in {"post_type", "self_id"}})
                protocol = OneBotClient(entry.config, entry.protocol)
                report = await backfill(entry.runtime, entry.client, protocol, asyncio.Event())
                assert report["added"] == 0 and report["coverage"] == "FULL"
                assert report["skipped"] == {"BEHIND_HELD_INPUTS": 1,
                                             "OLDER_THAN_CAPTURE_BOUND": 1}
                assert report["notices_recoverable"] is False
                await promote_all(entry)
                report = await backfill(entry.runtime, entry.client, protocol, asyncio.Event())
                assert report["added"] == 1
        finally:
            onebot.HISTORY.clear()

    run(scenario())


def test_a_changed_held_frame_fails_custody(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path) as (entry, _):
            core.clock.now += timedelta(seconds=30)
            await receive(entry, 41, "原文")
            with entry.db:
                entry.db.execute("UPDATE held_inputs SET frame=frame||' '")
            with pytest.raises(ValueError, match="OWNER_HELD_INPUT_PROJECTION_CHANGED"):
                entry.runtime.verify()

    run(scenario())


def test_the_receive_loop_holds_while_core_is_down_and_upload_admits_after(core, tmp_path):
    from companion_onebot_bridge import app
    from companion_onebot_bridge.metrics import Metrics

    class Flaky(httpx.AsyncBaseTransport):
        down = True

        def __init__(self, inner):
            self.inner = inner

        async def handle_async_request(self, request):
            if self.down:
                raise httpx.ConnectError("core restarting", request=request)
            return await self.inner.handle_async_request(request)

    async def scenario():
        async with environment(core, tmp_path) as (entry, _):
            # The receive loop stamps frames with the wall clock; keep Core's clock on it.
            core.clock.now = datetime.now(UTC)
            transport = Flaky(entry.client._transport)
            stages = []
            metrics = Metrics(sink=lambda record: stages.append(record["stage"]))
            protocol = OneBotClient(entry.config, entry.protocol)
            async with httpx.AsyncClient(base_url=entry.client.base_url, transport=transport,
                                         headers=entry.client.headers) as flaky:
                stop, wake = asyncio.Event(), asyncio.Event()
                frame = raw_event(51, text="重启中", time=int(core.clock.now.timestamp()))
                entry.frames.put_nowait(json.dumps(frame))
                receiving = asyncio.create_task(app.receive(
                    entry.config, entry.spool, wake, stop, metrics, protocol,
                    runtime=entry.runtime, core=flaky))
                for _ in range(200):
                    if held_inputs.waiting(entry.runtime):
                        break
                    await asyncio.sleep(0.05)
                assert held_inputs.waiting(entry.runtime)
                assert not receiving.done()  # Core being down no longer ends receiving.
                assert "core_unavailable" in stages
                transport.down = False
                core.clock.now = datetime.now(UTC)
                uploading = asyncio.create_task(app.upload(
                    flaky, entry.spool, wake, stop, metrics, runtime=entry.runtime,
                    onebot=protocol))
                for _ in range(200):
                    if entry.db.execute("SELECT 1 FROM inbox WHERE state='accepted'").fetchone():
                        break
                    await asyncio.sleep(0.05)
                stop.set()
                assert await app.drain([receiving, uploading])
            assert results(entry) == [("PROMOTED", None)]
            report = app.shutdown_report(entry.runtime, True)
            assert report == {"status": "CLEAN", "cancelled": False, "unfinished_sends": {},
                              "pending_inputs": 0, "pending_results": 0, "held_inputs": 0}

    run(scenario())


def test_an_unresolvable_core_name_is_a_transport_error():
    from companion_onebot_bridge.network import private_http_client

    # A stopped Compose service has no DNS record (rehearsal 2026-10-31: the delivery
    # loop crashed on socket.gaierror and the receive loop dropped the frame).
    async def scenario():
        async with private_http_client("http://companion-api.invalid:8765", "t" * 32) as client:
            with pytest.raises(httpx.ConnectError) as error:
                await client.post("/internal/v1/owner-authority/check", json={})
            assert held_inputs.transient(error.value)

    run(scenario())


def test_the_scheduler_loop_ends_on_stop_without_waiting_out_its_minute():
    from companion_onebot_bridge.scheduled_import import run_scheduler_loop

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(run_scheduler_loop(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 2)

    run(scenario())
