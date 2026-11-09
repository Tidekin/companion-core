"""Native owner contracts over authenticated WS and real Core HTTP.

Local pause is not authority STOP; unsupported notices do not stop a native
receiver; transport deadlines retain the original uncertainty and accounting.
"""

import asyncio
import json
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from companion import owner_authority
from companion.reply_segments import SegmentedReplyConfig
from companion_onebot_bridge import app, journal
from companion_onebot_bridge.owner_runtime import OwnerRuntime, command
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.native_continuous_support import answer, frame, host, recall_frame, until
from tests.native_continuous_support import worker as worker
from tests.support.onebot import onebot_http
from tests.support.segmented_image_memory import PARTS, REPLY


async def one_message(ws):
    await ws.send(frame())
    await ws.wait_closed()


async def silent(ws):
    await ws.wait_closed()


def test_two_rapid_messages_and_duplicate_share_one_authenticated_connection(core, tmp_path):
    connections = []

    async def source(ws):
        connections.append(ws)
        await ws.send(frame(10, user_id=10003, sender={"user_id": 10003}))
        first = frame(1)
        for item in (first, first, frame(2, "新的问题")):
            await ws.send(item)
        await ws.wait_closed()

    async def scenario():
        async with host(core, tmp_path, source) as (active, handshakes, sends, calls):
            active.start(deliver=False, worker=False)
            await until(lambda: core.store.one("SELECT COUNT(*) n FROM events")["n"] == 2,
                        tasks=active.tasks)
            assert len(connections) == len(handshakes) == 1
            assert handshakes == ["Bearer " + active.config.onebot_token]
            assert active.captures() == 2
            # The older input was never dispatched; Core owns its supersession.
            assert await core.run_once()
            assert not await core.run_once()
            assert await active.pump()
            assert len(calls) == len(sends) == 1 and active.attempts() == {"ACK": 1}
            jobs = core.store.rows(
                'SELECT state,last_error_code FROM jobs ORDER BY '
                'record_sequence'
            )
            assert jobs[0]["state"] == "CANCELLED" and jobs[1]["state"] == "SUCCEEDED"
            assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
            assert any(x["stage"] == "duplicate_suppressed" for x in active.events)
            active.runtime.verify()
    run(scenario())


def test_existing_reviewed_window_never_becomes_automatic(core, tmp_path):
    async def scenario():
        async with host(core, tmp_path, silent) as (active, handshakes, sends, _):
            with pytest.raises(ValueError, match="OWNER_AUTHORITY_REQUIRED"):
                await app.run(replace(active.config, owner_authority=None))
            cfg = replace(active.config, data_dir=str(tmp_path / "frozen-history"))
            with Spool(cfg) as historical:
                historical.db.execute("CREATE TABLE test_window(evidence TEXT)")
                historical.db.execute("INSERT INTO test_window VALUES(?)",
                    ('{"version":1,"state":"STOPPED","approval":"retained"}',))
                historical.db.commit()
                before = historical.db.execute("SELECT evidence FROM test_window").fetchall()
                with pytest.raises(ValueError, match="OWNER_CUSTODY_CUTOVER_REQUIRED"):
                    OwnerRuntime(historical)
                retained = historical.db.execute("SELECT evidence FROM test_window").fetchall()
                assert retained == before
            assert handshakes == sends == []
    run(scenario())


def test_idle_can_pause_and_resume_same_native_authority(core, tmp_path):
    connections = []

    async def source(ws):
        connections.append(ws)
        if len(connections) > 1:
            await ws.send(frame())
        await ws.wait_closed()

    async def scenario():
        async with host(core, tmp_path, source) as (active, _, sends, calls):
            original = active.runtime.authority
            active.start(worker=False)
            await until(lambda: bool(connections), tasks=active.tasks)
            await asyncio.sleep(0.15)
            assert all(not task.done() for task in active.tasks) and active.captures() == 0
            state = command(active.config, SimpleNamespace(command="owner-status"))
            assert state["state"] == "ACTIVE"
            assert await active.pause() == []
            active.restart()
            active.start()
            await until(lambda: active.attempts() == {"ACK": 1}, tasks=active.tasks)
            assert active.runtime.authority == original and len(calls) == len(sends) == 1
    run(scenario())


def test_idle_websocket_disconnect_reconnects_without_resetting_authority(core, tmp_path):
    connections = []

    async def source(ws):
        connections.append(ws)
        if len(connections) == 1:
            await ws.close()
        else:
            await ws.send(frame())
            await ws.wait_closed()

    async def scenario():
        async with host(core, tmp_path, source) as (active, handshakes, sends, calls):
            reference = active.runtime.authority
            active.start()
            await until(lambda: active.attempts() == {"ACK": 1}, tasks=active.tasks)
            assert len(connections) == len(handshakes) == 2
            assert active.captures() == len(calls) == len(sends) == 1
            assert active.runtime.authority == reference
    run(scenario())


@pytest.mark.parametrize("reason", ["stop", "authority_stop", "expiry"])
def test_idle_stop_and_expiry_fence_native_io_without_fabricated_core_confirmation(
    core, tmp_path, reason,
):
    async def scenario():
        async with host(core, tmp_path, silent, expires=True) as (active, seen, sends, calls):
            active.start(worker=False)
            await until(lambda: bool(seen), tasks=active.tasks)
            if reason == "stop":
                result = command(active.config, SimpleNamespace(command="owner-stop"))
                assert result["state"] == "STOP_REQUESTED" and not result["core_stop_confirmed"]
                await until(lambda: any(task.done() for task in active.tasks))
                with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                    active.runtime.verify()
                state = owner_authority.status(core.store, core.settings, core.clock)
                assert state["state"] == "ACTIVE"
            elif reason == "authority_stop":
                core.settings.control_token = SecretStr("synthetic-idle-stop-control")
                stopped = await active.client.post("/internal/v1/owner-authority/stop",
                    json={"authority": active.runtime.authority,
                          "operation_id": "native-idle-owner-stop"},
                    headers={"Authorization": "Bearer synthetic-idle-stop-control"})
                assert stopped.status_code == 200 and stopped.json()["state"] == "STOPPED"
                with pytest.raises(httpx.HTTPStatusError) as rejected:
                    await active.runtime.check(active.client)
                assert rejected.value.response.status_code == 403
            else:
                core.clock.advance(61)
                with pytest.raises(httpx.HTTPStatusError) as rejected:
                    await active.runtime.check(active.client)
                assert rejected.value.response.status_code == 403
                state = owner_authority.status(core.store, core.settings, core.clock)
                assert state["state"] == "OFF"
            assert active.captures() == 0 and sends == calls == []
            assert core.store.rows("SELECT * FROM model_usage") == []
    run(scenario())


def test_uncertain_delivery_stops_without_automatic_resend(core, tmp_path):
    attempted = []

    def protocol(request):
        if request.url.path == "/send_private_msg":
            attempted.append(request)
            raise httpx.ReadTimeout("synthetic unknown delivery", request=request)

    async def scenario():
        async with host(core, tmp_path, one_message, protocol_reply=protocol) as (
            active, _, sends, calls,
        ):
            active.start()
            await until(lambda: active.runtime.row()["state"] == "STOPPED", tasks=active.tasks)
            assert active.attempts() == {"UNKNOWN": 1}
            assert len(attempted) == len(calls) == 1 and sends == []
            assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"
            await active.pause()
            active.restart()
            with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                active.runtime.verify()
            assert not await active.spool.flush_result(active.client)
            assert len(attempted) == 1
    run(scenario())


@pytest.mark.parametrize("already_uploaded", [False, True])
def test_restart_resumes_durably_captured_or_uploaded_input_once(core, tmp_path, already_uploaded):
    async def scenario():
        async with host(core, tmp_path, one_message) as (active, _, sends, calls):
            await active.capture()
            if already_uploaded:
                await active.flush()
            active.restart()
            active.start(receive=False)
            await until(lambda: active.attempts() == {"ACK": 1}, tasks=active.tasks)
            assert active.captures() == len(calls) == len(sends) == 1
            assert core.store.one("SELECT COUNT(*) n FROM events")["n"] == 1
    run(scenario())


def test_authenticated_buffer_survives_receiver_restart_and_rejects_tampering(core, tmp_path):
    async def scenario():
        async with host(core, tmp_path, one_message) as (active, seen, sends, calls):
            await active.capture()
            active.restart()
            original = active.db.execute("SELECT body FROM inbox").fetchone()[0]
            changed = json.loads(original)
            changed["payload"]["text"] = "tampered authenticated input"
            with active.db:
                active.db.execute("UPDATE inbox SET body=?", (json.dumps(changed),))
            with pytest.raises(ValueError, match="OWNER_CAPTURE_PROJECTION_CHANGED"):
                await active.flush()
            assert core.store.rows("SELECT * FROM events") == []
            with active.db:
                active.db.execute("UPDATE inbox SET body=?", (original,))
            active.start(receive=False)
            await until(lambda: active.attempts() == {"ACK": 1}, tasks=active.tasks)
            assert len(seen) == len(calls) == len(sends) == 1
            assert active.db.execute("SELECT body FROM inbox").fetchone()[0] is None
    run(scenario())


def test_automatic_segmented_reply_preserves_order_and_uses_one_generation(core, tmp_path):
    async def scenario():
        async with host(core, tmp_path, one_message,
            policy=SegmentedReplyConfig(min_chars=12, interval_ms=0),
            model_reply=lambda _: answer(REPLY)) as (active, _, sends, calls):
            active.start()
            await until(lambda: active.attempts() == {"ACK": 3}, tasks=active.tasks)
            assert [item["message"][0]["data"]["text"] for item in sends] == PARTS
            assert active.captures() == len(calls) == 1
    run(scenario())


def test_owner_recall_and_unrelated_notices_do_not_interrupt_private_replies(core, tmp_path):
    async def source(ws):
        common = json.loads(recall_frame(123))
        for notice in [common,
            common | {"notice_type": "notify", "sub_type": "input_status", "event_type": 1},
            common | {"notice_type": "notify", "sub_type": "poke", "target_id": 10002},
            common | {"user_id": 10003, "message_id": 456}]:
            await ws.send(json.dumps(notice))
        await ws.send(frame(1))
        await ws.send(frame(2))
        await ws.wait_closed()

    async def scenario():
        async with host(core, tmp_path, source) as (active, _, sends, calls):
            active.start(worker=False, deliver=False)
            await until(lambda: core.store.one("SELECT COUNT(*) n FROM events")["n"] == 2,
                        tasks=active.tasks)
            assert await core.run_once()
            assert await active.pump()
            assert len(calls) == len(sends) == 1 and active.captures() == 2
            assert active.db.execute("SELECT COUNT(*) FROM automatic_recalls").fetchone()[0] == 1
            active.runtime.verify()
    run(scenario())


@pytest.mark.parametrize("fault", ["account", "input_status"])
def test_notice_routing_preserves_account_scope_and_ignores_unsupported_status(
    core, tmp_path, fault,
):
    async def source(ws):
        notice = json.loads(recall_frame(123))
        if fault == "account":
            notice["self_id"] = 10003
        else:
            notice.update(notice_type="notify", sub_type="input_status")
        await ws.send(json.dumps(notice))
        await ws.send(frame())
        await ws.wait_closed()

    async def scenario():
        async with host(core, tmp_path, source) as (active, _, sends, calls):
            active.start()
            await until(lambda: active.attempts() == {"ACK": 1}, tasks=active.tasks)
            assert active.captures() == len(sends) == len(calls) == 1
            assert core.store.one("SELECT COUNT(*) n FROM events")["n"] == 1
            assert not active.db.execute("SELECT 1 FROM sqlite_master "
                                         "WHERE name='automatic_recalls'").fetchone()
    run(scenario())


@pytest.mark.parametrize("resume", [False, True])
def test_progressing_long_reply_finishes_and_resume_keeps_native_segments(core, tmp_path, resume):
    policy = SegmentedReplyConfig(max_segments="unlimited", mode="semantic", interval_ms=600)

    async def scenario():
        async with host(core, tmp_path, one_message, policy=policy,
            model_reply=lambda _: answer("好呀\n接着说\n我在听\n慢慢聊")) as (
            active, _, sends, calls,
        ):
            await active.capture()
            await active.flush()
            assert await core.run_once()
            start = time.monotonic()
            if resume:
                assert await active.pump() and len(sends) == 1
                active.restart()
            active.start(receive=False, upload=False, worker=False)
            await until(lambda: active.attempts() == {"ACK": 4}, tasks=active.tasks)
            assert time.monotonic() - start >= 1.7
            assert len(calls) == 1
            assert [x["message"][0]["data"]["text"] for x in sends] == [
                "好呀", "接着说", "我在听", "慢慢聊"]
            assert not core.store.one("SELECT 1 FROM model_usage WHERE state='UNKNOWN'")
            active.runtime.verify()
    run(scenario())


@pytest.mark.parametrize("stage", ["model", "send"])
def test_provider_timeouts_remain_bounded_and_do_not_repeat_io(core, tmp_path, stage):
    attempts = []

    def timeout(request):
        if stage == "model" or request.url.path == "/send_private_msg":
            attempts.append(request)
            raise httpx.ReadTimeout("synthetic transport deadline", request=request)

    async def scenario():
        async with host(core, tmp_path, one_message,
            model_reply=timeout if stage == "model" else None,
            protocol_reply=timeout if stage == "send" else None) as (active, _, sends, _):
            await active.capture()
            await active.flush()
            started = time.monotonic()
            assert await core.run_once()
            if stage == "send":
                assert await active.pump()
                assert active.attempts() == {"UNKNOWN": 1}
                assert active.runtime.row()["state"] == "STOPPED"
            else:
                assert core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"
                assert core.store.rows("SELECT * FROM outbox") == []
            assert time.monotonic() - started < 5
            assert not await core.run_once() and len(attempts) == 1 and sends == []
    run(scenario())


@pytest.mark.parametrize("outcome", ["continue", "resume", "new_input", "recall", "stop"])
def test_segment_waits_for_bridge_ack_interval_after_core_result_response_delay(
    core, tmp_path, outcome,
):
    connections = []

    async def source(ws):
        connections.append(ws)
        if len(connections) == 1:
            await ws.send(frame())
        elif outcome == "new_input":
            await ws.send(frame(2, "等等，换个话题"))
        elif outcome == "recall":
            await ws.send(recall_frame())
        await ws.wait_closed()

    async def scenario():
        policy = SegmentedReplyConfig(max_segments="unlimited", mode="semantic", interval_ms=600)
        async with host(core, tmp_path, source, policy=policy, ticking=False,
            model_reply=lambda _: answer("好呀\n接着说")) as (active, _, sends, calls):
            await active.capture()
            await active.flush()
            assert await core.run_once()
            # Freeze a short future server interval so fresh real WS interruption
            # frames stay within capture drift while the ACK clock is controlled.
            core.clock.now = datetime.now(UTC) + timedelta(seconds=5)
            initial, delayed = core.clock.now, False

            async def delayed_result(response):
                nonlocal delayed
                if response.request.url.path.endswith("/result") and not delayed:
                    assert response.status_code == 200
                    core.clock.advance(0.1)
                    delayed = True

            active.client.event_hooks["response"].append(delayed_result)
            assert await active.pump() and len(sends) == 1
            first = core.store.one("SELECT sent_at FROM outbox WHERE segment_index=0")
            assert first["sent_at"] == initial.isoformat(timespec="microseconds")
            records, _ = journal.read(active.runtime.path)
            ack = next(item for item in records if item["kind"] == "ACK")
            assert ack["at"] == (initial + timedelta(seconds=0.1)).isoformat()
            if outcome == "resume":
                active.restart()
            core.clock.advance(0.5)
            await active.pump()
            # Core's interval elapsed; the actual local ACK still needs 100 ms.
            assert len(sends) == 1
            pending = core.store.one("SELECT state FROM outbox WHERE segment_index=1")
            assert pending["state"] == "PENDING" and active.attempts() == {"ACK": 1}
            if outcome in {"continue", "resume"}:
                core.clock.advance(0.1)
                assert await active.pump() and len(sends) == 2
                assert active.attempts() == {"ACK": 2}
            elif outcome == "stop":
                active.runtime.stop()
                active.start(receive=False, upload=False, worker=False)
                await until(lambda: active.tasks[0].done())
                with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                    active.tasks[0].result()
                assert len(sends) == 1
            else:
                active.start(upload=False, deliver=False, worker=False)
                await until(lambda: active.captures() == 2 if outcome == "new_input" else bool(
                    active.db.execute("SELECT 1 FROM sqlite_master WHERE name='automatic_recalls'")
                    .fetchone()), tasks=active.tasks)
                await active.pause()
                await active.flush()
                if outcome == "new_input":
                    assert await core.run_once()
                    assert await active.pump()
                    assert len(calls) == 2 and len(sends) == 2
                else:
                    assert not await active.pump() and len(sends) == 1
                remaining = core.store.one(
                    'SELECT state FROM outbox ORDER BY record_sequence LIMIT 1 OFFSET 1')
                assert remaining["state"] == "CANCELLED"
            assert core.clock.now <= initial + timedelta(seconds=0.7)
            assert not core.store.one("SELECT 1 FROM outbox WHERE state IN ('LEASED','SENDING')")
    run(scenario())


def test_websocket_auth_rejection_never_admits_an_input(core, tmp_path):
    async def scenario():
        async with host(core, tmp_path, silent, reject_auth=True) as (active, seen, sends, calls):
            active.start(upload=False, deliver=False, worker=False)
            await until(lambda: active.tasks[0].done())
            with pytest.raises(ValueError, match="ONEBOT_AUTH_REJECTED"):
                active.tasks[0].result()
            assert len(seen) == 1 and active.captures() == 0 and sends == calls == []
    run(scenario())


def test_native_fixture_explicit_scopes_and_independent_api_lifespan(core, tmp_path):
    purposes = ("chat", "memory", "history", "affect", "diary", "reflection", "goals",
                "exploration", "social", "proactive", "content")

    async def scenario():
        async with host(core, tmp_path, silent, independent_api=True, purposes=purposes,
            core_options={"memory_enabled": True, "affect_enabled": True,
                          "diary_enabled": True}) as (active, seen, sends, calls):
            assert active.api_core is not core
            assert (await active.runtime.check(active.client))["approved"]
            view = owner_authority.current(core.store, core.settings, core.clock)
            assert view.policy.purposes == list(purposes)
            assert view.policy.memory_scope == "owner_private"
            assert view.policy.history.scope == "owner_private"
            assert active.captures() == 0 and seen == sends == calls == []
    run(scenario())


@pytest.mark.parametrize("mutation", ["path", "stop"])
def test_protocol_request_hook_is_rechecked_before_external_send(core, tmp_path, mutation):
    async def scenario():
        async with host(core, tmp_path, one_message) as (active, _, sends, calls):
            await active.capture()
            await active.flush()
            assert await core.run_once()

            async def mutate(request):
                if request.url.path == "/send_private_msg":
                    if mutation == "path":
                        request.url = request.url.copy_with(path="/send_group_msg")
                    else:
                        active.runtime.stop()

            active.protocol.event_hooks["request"].append(mutate)
            assert await active.pump()
            assert sends == [] and len(calls) == 1
            # Core accepts narrow supersession/recall attestations. A local hook
            # refusal alone cannot manufacture NOT_SENT; retain uncertainty.
            assert active.attempts() == {"UNKNOWN": 1}
            assert active.runtime.row()["state"] == "STOPPED"
            assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"
    run(scenario())


def test_native_daemon_entrypoint_in_independent_process(core, tmp_path):
    async def scenario():
        async with host(core, tmp_path, one_message) as (active, handshakes, _, calls):
            sends = []
            with onebot_http(sends, active.config.onebot_token) as port:
                cfg = replace(active.config, onebot_http_url=f"http://127.0.0.1:{port}")
                config_path = tmp_path / "native-daemon.json"
                config_path.write_text(json.dumps(asdict(cfg)), encoding="utf-8")
                active.spool.close()
                active.start(receive=False, upload=False, deliver=False)
                child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
                    "tests.native_continuous_process", str(config_path),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
                try:
                    await until(lambda: bool(core.store.one(
                        "SELECT 1 FROM outbox WHERE state='SENT'")),
                        tasks=active.tasks, deadline_seconds=45)
                    def locally_acknowledged():
                        path = active.spool.directory / "spool.sqlite3"
                        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                            return db.execute("SELECT 1 FROM window_attempts WHERE state='ACK'")\
                                .fetchone() is not None

                    await until(locally_acknowledged, tasks=active.tasks)
                    async with asyncio.timeout(10):
                        output, error = await child.communicate(b"stop\n")
                    assert child.returncode == 0, error.decode(errors="replace")
                    result = json.loads(output.splitlines()[-1])
                    assert result == {"captures": 1, "attempts": {"ACK": 1}}
                    assert len(handshakes) == len(calls) == len(sends) == 1
                finally:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()
    run(scenario())
