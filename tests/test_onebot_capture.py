"""Real loopback WebSockets and synthetic identities only; no QQ/provider I/O."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import asynccontextmanager, nullcontext
from dataclasses import asdict, replace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed, InvalidStatus

from companion.api import create_app
from companion.config import LiveTextWindowApproval
from companion.model import ModelClient
from companion_onebot_bridge import capture, network
from companion_onebot_bridge.spool import Spool
from companion_onebot_bridge.window import SendOperation, TestWindow, WindowPlan, digest
from tests.conftest import run
from tests.journal_assertions import assert_private_journal
from tests.support.onebot import onebot_http, provider
from tests.support.onebot_events import config, raw_event
from tests.test_live_text_window import live_fixture
from tests.test_onebot_window import input_approval, plan_fixture, reply_approval, stage


def capture_approval(window):
    row = window.db.execute("SELECT * FROM window_inputs ORDER BY rowid DESC LIMIT 1").fetchone()
    return {
        "window_sha256": window.plan.sha256,
        "event_id": row["event_id"],
        "input_sha256": row["input_hash"],
        "capture_sha256": digest(json.loads(row["capture_json"])),
        "evidence_sha256": "e" * 64,
    }


def bootstrap_frames():
    common = {"time": raw_event()["time"], "self_id": 10002, "post_type": "meta_event"}
    return [
        common | {"meta_event_type": "lifecycle", "sub_type": kind}
        for kind in ("connect", "enable")
    ] + [
        common
        | {
            "meta_event_type": "heartbeat",
            "status": {"online": True, "good": True},
            "interval": 15000,
        }
    ]


def pinned_plan(
    core, tmp_path, ws_port, *, http_port=3000, core_port=8765, live=False, approved=False,
    rich_input_scope="none",
    scope_options=None,
    bridge_options=None,
):
    options = {
        "onebot_ws_url": f"ws://127.0.0.1:{ws_port}",
        "onebot_http_url": f"http://127.0.0.1:{http_port}",
        "core_url": f"http://127.0.0.1:{core_port}",
    }
    if approved:
        options["capture_signing_key"] = Ed25519PrivateKey.generate().private_bytes_raw().hex()
    options.update(bridge_options or {})
    if live:
        cfg, plan = live_fixture(core, tmp_path, bridge_options=options,
                                 rich_input_scope=rich_input_scope, **(scope_options or {}))
        value = dict(plan.value)
        inventory = {
            key: value[key]
            for key in (
                "identity_sha256",
                "protocol_epoch",
                "config_sha256",
                "config_version",
                "image_ids",
            )
        }
    else:
        cfg, value, inventory, now = plan_fixture(tmp_path)
        cfg = replace(cfg, **options)
        value["config_sha256"] = inventory["config_sha256"] = digest(cfg.snapshot())
        value["runtime_attestation_sha256"] = digest(inventory)
        core.clock.now = now
        core.settings.bridge_token = SecretStr(cfg.bridge_token)
    authorization = None
    if approved:
        value["execution"] = "approved_live"
        if live:
            value["live_text"] = {**value["live_text"], "execution": "approved_live"}
        authorization = {
            "kind": "owner_qq_window",
            "approved": True,
            "scope_sha256": digest(
                {key: val for key, val in value.items() if key != "authorization_sha256"}
            ),
            "evidence_sha256": "a" * 64,
        }
        value["authorization_sha256"] = digest(authorization)
    plan = WindowPlan(value, cfg, inventory, authorization)
    if live:
        core.settings.live_text_window = LiveTextWindowApproval(
            **value["live_text"], plan_sha256=plan.sha256
        )
    return cfg, plan


@asynccontextmanager
async def source_window(
    core, tmp_path, source, *, live=False, approved=False, reject_auth=False, protocol_reply=None,
    rich_input_scope="none",
    scope_options=None,
    bridge_options=None,
    independent_api=False,
):
    handshakes, sends = [], []

    async def authenticate(connection, request):
        handshakes.append(request.headers.get("Authorization"))
        if reject_auth or request.headers.get("Authorization") != "Bearer " + config().onebot_token:
            return connection.respond(401, "synthetic auth required")

    async with serve(source, "127.0.0.1", 0, process_request=authenticate) as server:
        cfg, plan = pinned_plan(
            core, tmp_path, server.sockets[0].getsockname()[1], live=live, approved=approved,
            rich_input_scope=rich_input_scope,
            scope_options=scope_options,
            bridge_options=bridge_options,
        )

        def respond(request):
            if protocol_reply:
                result = protocol_reply(request)
                if result is not None:
                    return result
            return provider(request, sends)

        api = create_app(core.settings)
        if not independent_api:
            api.state.core = core
        async with (
            api.router.lifespan_context(api) if independent_api else nullcontext(),
            network.private_http_client(
                cfg.core_url, cfg.bridge_token, transport=httpx.ASGITransport(app=api)
            ) as client,
            network.private_http_client(
                cfg.onebot_http_url, cfg.onebot_token, transport=httpx.MockTransport(respond)
            ) as protocol,
        ):
            with Spool(cfg) as spool:
                window = TestWindow(spool, plan, clock=lambda: core.clock.now)
                await window.open(client, protocol if approved else None)
                yield window, client, protocol, handshakes, sends


@pytest.mark.parametrize("restart", [False, True])
def test_authenticated_ws_real_http_five_input_model_outbox_send_chain(core, tmp_path, restart):
    from companion_onebot_bridge.owner_runtime import OwnerRuntime
    from tests.support.native_context import source_window as native_source

    number, calls = 1, []

    async def source(ws):
        for frame in bootstrap_frames():
            await ws.send(json.dumps(frame))
        await ws.send(json.dumps(raw_event(number)))
        await ws.wait_closed()

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "合成回答"}}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    async def scenario():
        nonlocal number
        async with native_source(core, tmp_path, source) as values:
            captured, client, protocol, peers, sends = values
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as model:
                core.model = ModelClient(core.settings, model)
                for number in range(1, 6):
                    if restart:
                        captured.runtime = OwnerRuntime(captured.spool,
                                                        clock=lambda: core.clock.now)
                    assert (await captured.capture_once(client, protocol))["new_inputs"] == 1
                    assert captured.spool.pending_count == 1
                    assert core.store.one("SELECT COUNT(*) n FROM events")["n"] == number - 1
                    assert await captured.runtime.flush_one(client)
                    assert await core.run_once("native-five-turn")
                    result = await captured.send(client, protocol)
                    assert result["attempt_states"] == {"ACK": number}
                    assert (await captured.capture_once(client, protocol))["new_inputs"] == 0
                    assert not await captured.runtime.flush_one(client)
                assert len(calls) == len(sends) == 5 and len(peers) == 10
                assert_private_journal(captured.runtime.path.read_text('utf-8'), (
                    captured.config.owner_qq_id, captured.config.bot_qq_id,
                    captured.config.onebot_token, "你好", captured.config.onebot_ws_url))
                assert not list((captured.spool.directory / "captures").glob("*.json"))
                assert len(core.store.rows("SELECT value FROM runtime_meta "
                                          "WHERE key LIKE 'owner_input:%'")) == 5
    run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": 10003},
        {"self_id": 10003},
        {"sender": {"user_id": 10003}},
        {"message_type": "group", "group_id": 10003},
        {"post_type": "message_sent"},
        {
            "message": [
                {"type": "reply", "data": {"id": "42"}},
                {"type": "text", "data": {"text": "/遗忘全部"}},
            ]
        },
        {"text": "/当前交互状态"},
        {"message": "[CQ:reply,id=42]普通文字"},
        {"message": [{"type": "image", "data": {"file": "synthetic"}}]},
        {"time": raw_event()["time"] - 31},
        {"time": raw_event()["time"] + 1},
        {"message_id": 0},
    ],
)
def test_wrong_identity_quote_media_control_stale_and_future_stop_before_core(
    core, tmp_path, change
):
    async def source(ws):
        await ws.send(json.dumps(raw_event(1, **change)))

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, sends):
            with pytest.raises(ValueError):
                await window.capture_once(client, protocol)
            assert window.status()["state"] == "STOPPED" and sends == []
            assert window.status()["new_inputs"] == 0
            assert core.store.one("SELECT COUNT(*) n FROM events")["n"] == 0

    run(scenario())


@pytest.mark.parametrize("fault", ["auth", "version", "identity", "disconnect", "clock"])
def test_bad_auth_probe_disconnect_and_clock_drift_are_persistent(core, tmp_path, fault):
    async def source(ws):
        if fault == "disconnect":
            await ws.close()
        else:
            if fault == "clock":
                core.clock.advance(3)
            await ws.send(json.dumps(raw_event(1)))

    def response(request):
        if fault == "version" and request.url.path == "/get_version_info":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "retcode": 0,
                    "data": {"app_name": "wrong", "app_version": "0"},
                },
            )
        if fault == "identity" and request.url.path == "/get_login_info":
            return httpx.Response(
                200, json={"status": "ok", "retcode": 0, "data": {"user_id": 10003}}
            )

    async def scenario():
        async with source_window(
            core, tmp_path, source, reject_auth=fault == "auth", protocol_reply=response
        ) as (window, client, protocol, calls, sends):
            with pytest.raises((ValueError, ConnectionClosed, InvalidStatus)):
                await window.capture_once(client, protocol)
            assert window.status()["state"] == "STOPPED" and sends == []
            prior = len(calls)
            with pytest.raises(ValueError):
                await window.capture_once(client, protocol)
            assert len(calls) == prior
            if fault in {"version", "identity"}:
                assert calls == []

    run(scenario())


@pytest.mark.parametrize("mutation", ["evidence", "body", "receipt", "approval"])
def test_approval_uses_only_original_persisted_frame_and_journal(core, tmp_path, mutation):
    async def source(ws):
        await ws.send(json.dumps(raw_event(1)))

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, sends):
            await window.capture_once(client, protocol)
            approval = capture_approval(window)
            row = window.db.execute("SELECT * FROM window_inputs").fetchone()
            receipt = json.loads(row["capture_json"])
            if mutation == "evidence":
                evidence = (
                    window.spool.directory / "captures" / (receipt["evidence_sha256"] + ".json")
                )
                evidence.write_text("{}", "utf-8")
            elif mutation == "body":
                window.db.execute("UPDATE inbox SET body='{}'")
                window.db.commit()
            elif mutation == "receipt":
                receipt["source_event_id"] += "7"
                window.db.execute("UPDATE window_inputs SET capture_json=?", (json.dumps(receipt),))
                window.db.commit()
            else:
                approval["capture_sha256"] = "0" * 64
            with pytest.raises(ValueError):
                await window.approve_captured(client, approval)
            assert window.status()["new_inputs"] == 1 and window.status()["state"] == "STOPPED"
            assert core.store.one("SELECT COUNT(*) n FROM events")["n"] == 0 and sends == []

    run(scenario())


def test_source_conflict_stops_after_exactly_one_input_and_send(core, tmp_path):
    text = ["你好"]

    async def source(ws):
        await ws.send(json.dumps(raw_event(1, text=text[0])))

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, sends):
            await window.capture_once(client, protocol)
            await window.approve_captured(client, capture_approval(window))
            assert await core.run_once()
            await window.send_once(client, protocol, reply_approval(window.plan, core))
            text[0] = "冲突正文"
            with pytest.raises(ValueError, match="SOURCE_EVENT_CONFLICT"):
                await window.capture_once(client, protocol)
            assert window.status()["new_inputs"] == window.status()["attempts_reserved"] == 1
            assert window.status()["state"] == "STOPPED" and len(sends) == 1

    run(scenario())


def test_capture_holds_original_os_lock_and_cli_stop_interrupts_receive(core, tmp_path):
    connected = asyncio.Event()

    async def source(ws):
        connected.set()
        await ws.wait_closed()

    async def scenario():
        async with source_window(core, tmp_path, source) as (
            window,
            client,
            protocol,
            calls,
            sends,
        ):
            task = asyncio.create_task(window.capture_once(client, protocol))
            await asyncio.wait_for(connected.wait(), 5)
            with pytest.raises(RuntimeError, match="BRIDGE_ALREADY_RUNNING"):
                with Spool(window.plan.config):
                    pytest.fail("spool lock must exclude another executor")
            with pytest.raises(ValueError, match="WINDOW_EXECUTOR_BUSY"):
                await window.capture_once(client, protocol)
            path = tmp_path / "bridge.toml"
            path.write_text(
                "\n".join(
                    f"{key} = {json.dumps(value)}"
                    for key, value in asdict(window.plan.config).items()
                    if value is not None
                ),
                "utf-8",
            )
            if os.name != "nt":
                path.chmod(0o600)
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    sys.executable,
                    "-m",
                    "companion_onebot_bridge",
                    "--config",
                    str(path),
                    "window-stop",
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                timeout=10,
            )
            assert result.returncode == 1 and json.loads(result.stdout)["state"] == "STOP_REQUESTED"
            with pytest.raises(ValueError, match="WINDOW_STOPPED"):
                await asyncio.wait_for(task, 5)
            assert window.status()["state"] == "STOPPED" and sends == [] and len(calls) == 1

    run(scenario())


def test_full_spool_blocks_before_any_probe_or_websocket(core, tmp_path):
    async def source(ws):
        pytest.fail("full spool must not connect")

    async def scenario():
        async with source_window(core, tmp_path, source) as (
            window,
            client,
            protocol,
            calls,
            sends,
        ):
            directory = window.spool.directory / "captures"
            directory.mkdir()
            (directory / "full.json").write_bytes(b"X" * window.plan.config.max_disk_bytes)
            with pytest.raises(ValueError, match="SPOOL_DISK_CAPACITY"):
                await window.capture_once(client, protocol)
            assert calls == sends == [] and window.status()["new_inputs"] == 0

    run(scenario())


@pytest.mark.parametrize("route", ["ws", "probe"])
def test_synthetic_dns_pin_nonloopback_is_rejected_before_authenticated_io(
    core,
    tmp_path,
    monkeypatch,
    route,
):
    async def source(ws):
        pytest.fail("nonloopback synthetic pin must not connect")

    async def scenario():
        async with source_window(core, tmp_path, source) as (
            window,
            client,
            protocol,
            calls,
            sends,
        ):

            async def resolve(host, port):
                return (
                    "192.168.77.9"
                    if route == "probe"
                    or port == int(window.plan.config.onebot_ws_url.rsplit(":", 1)[1])
                    else "127.0.0.1"
                )

            monkeypatch.setattr(network, "resolve_private", resolve)
            if route == "probe":
                async with network.private_http_client(
                    window.plan.config.onebot_http_url, window.plan.config.onebot_token
                ) as pinned:
                    with pytest.raises(ValueError):
                        await window.capture_once(client, pinned)
            else:
                with pytest.raises(ValueError, match="WEBSOCKET_DESTINATION_REFUSED"):
                    await window.capture_once(client, protocol)
            assert calls == sends == [] and window.status()["new_inputs"] == 0

    run(scenario())


def test_approved_live_staged_json_cannot_substitute_for_ws(core, tmp_path):
    async def source(ws):
        pytest.fail("staged upload must not connect")

    async def scenario():
        async with source_window(core, tmp_path, source, live=True, approved=True) as (
            window,
            client,
            protocol,
            calls,
            sends,
        ):
            approval = input_approval(window.plan, raw_event(1))
            response = await client.post("/internal/v1/live-text/inputs/approve", json=approval)
            assert response.status_code == 403
            assert response.json()["error"] == "LIVE_TEXT_AUTHENTICATED_CAPTURE_REQUIRED"
            with pytest.raises(ValueError, match="AUTHENTICATED_CAPTURE_REQUIRED"):
                await window.ingest(client, raw_event(1), approval)
            assert window.status()["new_inputs"] == 0 and calls == sends == []

    run(scenario())


def test_old_spool_cannot_erase_a_captured_input_or_reopen_its_allowance(core, tmp_path):
    async def source(ws):
        await ws.send(json.dumps(raw_event(1)))

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, _):
            snapshot = tmp_path / "old-spool.sqlite3"
            with sqlite3.connect(snapshot) as old:
                window.db.backup(old)
            await window.capture_once(client, protocol)
            assert window.status()["new_inputs"] == 1
            cfg, plan = window.plan.config, window.plan
        with (
            sqlite3.connect(snapshot) as old,
            sqlite3.connect(tmp_path / "spool" / "spool.sqlite3") as restored,
        ):
            old.backup(restored)
        with Spool(cfg) as spool:
            restarted = TestWindow(spool, plan, clock=lambda: core.clock.now)
            with pytest.raises(ValueError, match="WINDOW_JOURNAL_RECONCILIATION_REQUIRED"):
                restarted.status()

    run(scenario())


def test_disconnect_with_core_stop_unconfirmed_remains_blocked_after_restart(core, tmp_path):
    async def source(ws):
        await ws.close()

    async def scenario():
        async with source_window(core, tmp_path, source, live=True) as (
            window,
            client,
            protocol,
            calls,
            sends,
        ):

            class LostStop(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    if request.url.path == "/internal/v1/live-text/stop":
                        raise httpx.ConnectError("synthetic stop unavailable")
                    return await client._transport.handle_async_request(request)

            async with httpx.AsyncClient(
                base_url=client.base_url, headers=client.headers, transport=LostStop()
            ) as disconnected:
                with pytest.raises(ValueError, match="LIVE_TEXT_CORE_STOP_UNCONFIRMED"):
                    await window.capture_once(disconnected, protocol)
            assert window.status()["state"] == "CORE_STOP_UNCONFIRMED"
            assert window.status()["local_state"] == "STOPPED" and sends == []
            cfg, plan = window.plan.config, window.plan
        with Spool(cfg) as spool:
            restarted = TestWindow(spool, plan, clock=lambda: core.clock.now)
            assert restarted.status()["state"] == "CORE_STOP_UNCONFIRMED"
            assert restarted.status()["new_inputs"] == 0 and len(calls) == 1

    run(scenario())


@pytest.mark.parametrize("change", ["identity", "source", "frame_time", "text_hash"])
def test_core_independently_validates_capture_receipt_binding(core, tmp_path, change):
    async def source(ws):
        await ws.send(json.dumps(raw_event(1)))

    async def scenario():
        async with source_window(core, tmp_path, source, live=True, approved=True) as (
            window,
            client,
            protocol,
            _,
            sends,
        ):
            await window.capture_once(client, protocol)
            row = window.db.execute("SELECT * FROM window_inputs").fetchone()
            receipt = json.loads(row["capture_json"])
            if change == "identity":
                receipt["identity_sha256"] = "0" * 64
            elif change == "source":
                receipt["source_event_id"] += "7"
            elif change == "frame_time":
                receipt["occurred_at"] = "2000-01-01T00:00:00+00:00"
            else:
                receipt["input_sha256"] = "0" * 64
            receipt["signature"] = (
                Ed25519PrivateKey.from_private_bytes(
                    bytes.fromhex(window.plan.config.capture_signing_key)
                )
                .sign(
                    capture.SIGNATURE_DOMAIN
                    + json.dumps(
                        {key: val for key, val in receipt.items() if key != "signature"},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                )
                .hex()
            )
            body = input_approval(window.plan, raw_event(1)) | {"capture": receipt}
            response = await client.post("/internal/v1/live-text/inputs/approve", json=body)
            assert response.status_code == 403
            assert response.json()["error"] == "LIVE_TEXT_CAPTURE_BINDING_CHANGED" and sends == []
            assert core.store.one("SELECT COUNT(*) n FROM events")["n"] == 0

    run(scenario())


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "window",
        "epoch",
        "config",
        "frame",
        "source",
        "input",
        "received_at",
        "evidence",
        "key",
    ],
)
def test_normal_http_token_cannot_forge_or_change_receiver_attestation(core, tmp_path, mutation):
    async def source(ws):
        await ws.send(json.dumps(raw_event(1)))

    async def scenario():
        async with source_window(core, tmp_path, source, live=True, approved=True) as (
            window,
            client,
            protocol,
            calls,
            sends,
        ):
            await window.capture_once(client, protocol)
            receipt = json.loads(
                window.db.execute("SELECT capture_json FROM window_inputs").fetchone()[0]
            )
            if mutation == "missing":
                receipt.pop("signature")
            elif mutation == "key":
                receipt["signature"] = Ed25519PrivateKey.generate().sign(b"forged").hex()
            elif mutation == "epoch":
                receipt["protocol_epoch"] = "other-epoch"
            elif mutation == "source":
                receipt["source_event_id"] += "7"
            elif mutation == "received_at":
                receipt["received_at"] = "2026-10-19T04:00:01+00:00"
            else:
                key = {
                    "window": "window_sha256",
                    "config": "config_sha256",
                    "frame": "frame_sha256",
                    "input": "input_sha256",
                    "evidence": "evidence_sha256",
                }[mutation]
                receipt[key] = "0" * 64
            body = input_approval(window.plan, raw_event(1)) | {"capture": receipt}
            response = await client.post("/internal/v1/live-text/inputs/approve", json=body)
            assert response.status_code == 403
            assert response.json()["error"] == "LIVE_TEXT_CAPTURE_SIGNATURE_REQUIRED"
            assert core.store.one("SELECT COUNT(*) n FROM events")["n"] == 0
            assert core.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == 0
            assert len(calls) == 1 and sends == []

    run(scenario())


def test_unsigned_staged_json_without_any_ws_or_journal_capture_cannot_reach_core(core, tmp_path):
    async def source(ws):
        pytest.fail("handwritten input must never be a receive-boundary call")

    async def scenario():
        async with source_window(core, tmp_path, source, live=True, approved=True) as (
            window,
            client,
            _protocol,
            calls,
            sends,
        ):
            from tests.support.onebot_events import normalized

            event = normalized(raw_event(1), window.plan.config)
            approval = input_approval(window.plan, raw_event(1))
            receipt = {
                "kind": "authenticated_onebot_ws",
                "window_sha256": window.plan.sha256,
                "event_id": event["event_id"],
                "source_event_id": event["source_event_id"],
                "input_sha256": approval["input_sha256"],
                "frame_sha256": "a" * 64,
                "received_at": core.clock(),
                "occurred_at": event["occurred_at"],
                "protocol_epoch": window.plan.config.protocol_epoch,
                "identity_sha256": window.plan.value["identity_sha256"],
                "config_sha256": window.plan.value["config_sha256"],
                "evidence_sha256": "e" * 64,
            }
            response = await client.post(
                "/internal/v1/live-text/inputs/approve",
                json=approval
                | {
                    "capture": receipt,
                },
            )
            assert response.status_code == 403
            assert response.json()["error"] == "LIVE_TEXT_CAPTURE_SIGNATURE_REQUIRED"
            response = await client.post(
                "/internal/v1/inbox/events",
                json=event,
                headers={"X-Idempotency-Key": event["event_id"]},
            )
            assert response.status_code == 403
            assert window.status()["new_inputs"] == 0 and calls == sends == []
            records, _ = window.read_journal()
            assert not any(row["kind"] == "CAPTURED" for row in records)
            assert core.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == 0

    run(scenario())


@pytest.mark.parametrize("erased", [False, True])
def test_accepted_or_erased_capture_clears_frame_text_and_repairs_interrupted_purge(
    core,
    tmp_path,
    monkeypatch,
    erased,
):
    async def source(ws):
        await ws.send(json.dumps(raw_event(1)))

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, sends):
            await window.capture_once(client, protocol)
            directory = window.spool.directory / "captures"
            assert len(list(directory.glob("*.json"))) == 1
            if erased:
                from companion.contracts import InboundEvent
                from tests.support.onebot_events import normalized

                event = normalized(raw_event(1), window.plan.config)
                core.ingest(InboundEvent.model_validate(event), event["event_id"])
                with core.store.transaction():
                    core.ledger.expire_events(core.store, [event["event_id"]], core.clock())
            # Emulate a crash after the durable ACK/body purge but before file removal.
            monkeypatch.setattr(window.spool, "remove_capture_text", lambda event_id: None)
            await window.approve_captured(client, capture_approval(window))
            row = window.db.execute("SELECT state,body FROM inbox").fetchone()
            assert row["state"] == ("erased" if erased else "accepted") and row["body"] is None
            assert len(list(directory.glob("*.json"))) == 1 and sends == []
            cfg = window.plan.config
        with Spool(cfg) as spool:
            assert list(directory.glob("*.json")) == []
            assert spool.pending_count == 0
            assert spool.db.execute("SELECT capture_json FROM window_inputs").fetchone()[0]

    run(scenario())


@pytest.mark.parametrize("change", ["deadline", "message", "stop"])
def test_final_dispatch_fence_runs_after_core_send_check_and_last_journal_read(
    core, tmp_path, monkeypatch, change,
):
    from tests.support.native_dispatch import attempts, dispatch, staged

    async def scenario():
        async with staged(core, tmp_path) as (owner, client, protocol, guard, sends):
            checked = []
            async def response(value):
                if value.request.url.path.endswith("/owner-authority/send-check"):
                    await value.aread()
                    assert value.status_code == 200
                    checked.append(True)
            client.event_hooks["response"].append(response)
            original = owner.verify
            changed = False
            def last_read(**options):
                nonlocal changed
                result = original(**options)
                if checked and not changed:
                    changed = True
                    if change == "deadline":
                        core.clock.now = guard.lease_until
                    elif change == "message":
                        guard.prepared_message = []
                    else:
                        owner.stop()
                return result
            monkeypatch.setattr(owner, "verify", last_read)
            await dispatch(owner, client, protocol, guard)
            assert checked == [True] and not sends and not guard.dispatched
            assert attempts(owner) == {"UNKNOWN": 1}
            assert owner.row()["state"] == "STOPPED"
    run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"sub_type": "disable"},
        {"self_id": 10003},
        {"time": raw_event()["time"] - 31},
        {"user_id": 10001, "message": "/遗忘全部"},
    ],
)
def test_malformed_or_unsafe_lifecycle_does_not_get_skipped(core, tmp_path, change):
    async def source(ws):
        await ws.send(json.dumps(bootstrap_frames()[0] | change))
        await ws.send(json.dumps(raw_event(1)))

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, _):
            with pytest.raises(ValueError, match="INVALID_CAPTURE_META_EVENT"):
                await window.capture_once(client, protocol)
            assert window.status()["new_inputs"] == 0

    run(scenario())


@pytest.mark.parametrize(
    "status",
    [{"online": False, "good": True}, {"online": True, "good": False}, {"online": 1, "good": True}],
)
def test_unhealthy_heartbeat_stops_before_owner_input(core, tmp_path, status):
    async def source(ws):
        await ws.send(json.dumps(bootstrap_frames()[2] | {"status": status}))

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, _):
            with pytest.raises(ValueError, match="INVALID_CAPTURE_META_EVENT"):
                await window.capture_once(client, protocol)
            assert window.status()["new_inputs"] == 0

    run(scenario())


@pytest.mark.parametrize("limit", ["frames", "bytes", "deadline"])
def test_meta_events_share_one_total_deadline_and_frame_byte_caps(
    core, tmp_path, monkeypatch, limit
):
    async def source(ws):
        frame = bootstrap_frames()[2]
        if limit == "bytes":
            frame["status"]["padding"] = "X" * 65000
        try:
            # Three large frames exceed the byte cap without queuing unread frames at close.
            for _ in range(3 if limit == "bytes" else 17):
                await ws.send(json.dumps(frame))
                if limit == "deadline":
                    await asyncio.sleep(0.06)
        except ConnectionClosed:
            pass

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, _):
            if limit == "deadline":
                monkeypatch.setattr(capture, "CAPTURE_WAIT_SECONDS", 0.15)
                with pytest.raises(TimeoutError):
                    await window.capture_once(client, protocol)
            else:
                expected = "CAPTURE_FRAME_CAP" if limit == "frames" else "CAPTURE_BYTE_CAP"
                with pytest.raises(ValueError, match=expected):
                    await window.capture_once(client, protocol)
            assert window.status()["state"] == "STOPPED" and window.status()["new_inputs"] == 0

    run(scenario())


@pytest.mark.parametrize("fault", ["waiting", "raw"])
def test_hard_exit_during_receive_or_raw_fsync_cannot_resume_or_restore_quota(
    core, tmp_path, fault
):
    connected = asyncio.Event()

    async def source(ws):
        connected.set()
        if fault == "raw":
            await ws.send(json.dumps(raw_event(1)))
        await ws.wait_closed()

    async def scenario():
        async with source_window(core, tmp_path, source) as (
            window,
            client,
            protocol,
            calls,
            sends,
        ):
            cfg, plan = window.plan.config, window.plan
            inventory = {
                key: plan.value[key]
                for key in (
                    "identity_sha256",
                    "protocol_epoch",
                    "config_sha256",
                    "config_version",
                    "image_ids",
                )
            }
            window.spool.close()
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tests.support.capture_child",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            child.stdin.write(
                (
                    json.dumps(
                        {
                            "config": asdict(cfg),
                            "plan": plan.value,
                            "inventory": inventory,
                            "now": core.clock(),
                            "fault": fault,
                        }
                    )
                    + "\n"
                ).encode()
            )
            await child.stdin.drain()
            child.stdin.close()
            try:
                await asyncio.wait_for(connected.wait(), 10)
                if fault == "waiting":
                    child.kill()
                await asyncio.wait_for(child.wait(), 10)
                if fault == "raw":
                    assert child.returncode == 23
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            with Spool(cfg) as spool:
                restarted = TestWindow(spool, plan, clock=lambda: core.clock.now)
                if fault == "waiting":
                    assert restarted.status()["state"] == "CAPTURE_UNCONFIRMED"
                    assert restarted.status()["new_inputs"] == 0
                    with pytest.raises(ValueError, match="CAPTURE_RECONCILIATION_REQUIRED"):
                        await restarted.capture_once(client, protocol)
                    assert restarted.status()["state"] == "STOPPED"
                else:
                    with pytest.raises(ValueError, match="WINDOW_JOURNAL_RECONCILIATION_REQUIRED"):
                        restarted.status()
                    assert len(list((spool.directory / "captures").glob("*.json"))) == 1
                assert len(calls) == 1 and sends == []

    run(scenario())


@pytest.mark.parametrize("live", [False])
@pytest.mark.parametrize("path", ["/get_login_info", "/get_status", "/get_version_info"])
@pytest.mark.parametrize("change", ["deadline", "stop"])
def test_every_send_probe_checks_window_after_dns_wait(core, tmp_path, live, path, change):
    dispatched = []

    async def source(ws):
        pytest.fail("send-once must not start another receiver")

    async def scenario():
        async with source_window(
            core,
            tmp_path,
            source,
            live=live,
            protocol_reply=lambda request: dispatched.append(request.url.path),
        ) as (
            window,
            client,
            protocol,
            _,
            sends,
        ):
            from companion_onebot_bridge.window import request_stop
            from tests.test_live_text_window import live_clients

            async with live_clients(core, window.plan.config) if live else nullcontext():
                approval = await stage(window, client, core)

                async def after_dns(request):
                    if request.url.path == path:
                        if change == "deadline":
                            core.clock.now = window.plan.end
                        else:
                            request_stop(window.spool.directory)

                protocol.event_hooks["request"].append(after_dns)
                with pytest.raises(ValueError):
                    await window.send_once(client, protocol, approval)
                assert path not in dispatched and sends == []
                assert window.status()["attempt_states"] == {"RESERVED": 1}
                assert window.status()["state"] == "STOPPED"

    run(scenario())


def test_demo_send_rechecks_actual_outbox_lease_after_dns(core, tmp_path):
    async def source(ws):
        pytest.fail("send-once must not start another receiver")

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, sends):
            approval = await stage(window, client, core)

            async def after_dns(request):
                if request.url.path == "/send_private_msg":
                    core.clock.advance(core.settings.lease_seconds + 1)
                    assert core.clock.now < window.plan.end

            protocol.event_hooks["request"].append(after_dns)
            status = await window.send_once(client, protocol, approval)
            assert sends == [] and status["attempt_states"] == {"UNKNOWN": 1}
            assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"

    run(scenario())


@pytest.mark.parametrize("change", ["body", "authorization", "host", "sni", "destination"])
def test_actual_send_request_is_revalidated_after_core_send_check(
    core, tmp_path, monkeypatch, change,
):
    from tests.support.native_dispatch import attempts, dispatch, staged

    async def scenario():
        async with staged(core, tmp_path) as (owner, client, protocol, guard, sends):
            pending = []
            async def remember(request):
                if request.url.path == "/send_private_msg":
                    pending.append(request)
            protocol.event_hooks["request"].append(remember)
            async def after_check(response):
                if response.request.url.path.endswith("/owner-authority/send-check"):
                    await response.aread()
                    assert response.status_code == 200
                    request = pending[0]
                    if change == "body":
                        request._content = b"{}"
                        request.stream = httpx.ByteStream(request.content)
                    elif change == "authorization":
                        request.headers["Authorization"] = "Bearer wrong-token"
                    elif change == "host":
                        request.headers["Host"] = "changed.invalid:3000"
                    elif change == "sni":
                        request.extensions["sni_hostname"] = "changed.invalid"
                    else:
                        request.url = request.url.copy_with(host="192.168.77.9")
            client.event_hooks["response"].append(after_check)
            await dispatch(owner, client, protocol, guard)
            assert len(pending) == 1 and not sends and not guard.dispatched
            assert attempts(owner) == {"UNKNOWN": 1}
    run(scenario())


@pytest.mark.parametrize("live", [False])
@pytest.mark.parametrize("change", ["text", "message_id", "lease_token", "delivery_key"])
def test_final_send_matches_persisted_reservation_despite_mutable_item_and_body(
    core,
    tmp_path,
    monkeypatch,
    live,
    change,
):
    operations = []
    original_reserve = SendOperation.reserve

    def remember(operation, item):
        original_reserve(operation, item)
        operations.append(operation)

    monkeypatch.setattr(SendOperation, "reserve", remember)

    async def source(ws):
        pytest.fail("send-once must not start another receiver")

    async def scenario():
        async with source_window(core, tmp_path, source, live=live) as (
            window,
            client,
            protocol,
            _,
            sends,
        ):
            from tests.test_live_text_window import live_clients

            async with live_clients(core, window.plan.config) if live else nullcontext():
                approval = await stage(window, client, core)
                original_message_id = approval["message_id"]

                async def mutate(request):
                    if request.url.path != "/send_private_msg":
                        return
                    item = operations[0].current_item
                    if change == "text":
                        text = "synthetic text outside the frozen individual reply approval"
                        item["content"]["text"] = text
                        body = json.loads(request.content)
                        body["message"][0]["data"]["text"] = text
                        request._content = json.dumps(body).encode()
                        request.headers["Content-Length"] = str(len(request.content))
                        request.stream = httpx.ByteStream(request.content)
                    elif change == "message_id":
                        item["message_id"] = "msg_" + "e" * 32
                    elif change == "lease_token":
                        item["lease_token"] = "lease_" + "e" * 32
                    else:
                        item["delivery_key"] = "reply:evt_" + "e" * 64 + ":v1"

                protocol.event_hooks["request"].append(mutate)
                status = await window.send_once(client, protocol, approval)
                assert sends == []
                assert status["attempts_reserved"] == 1
                assert status["attempt_states"] == {"UNKNOWN": 1}
                assert status["state"] == "STOPPED"
                attempt = window.db.execute("SELECT * FROM window_attempts").fetchone()
                assert attempt["message_id"] == original_message_id
                assert attempt["text_hash"] == approval["reply_sha256"]
                assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"

    run(scenario())


@pytest.mark.parametrize("live", [False])
@pytest.mark.parametrize("change", ["stream", "transfer_encoding", "unbounded_stream"])
def test_final_wire_bytes_match_approval_when_only_httpx_stream_is_replaced(
    core,
    tmp_path,
    live,
    change,
):
    sends = []

    async def scenario():
        with onebot_http(sends, config().onebot_token) as port:
            cfg, plan = pinned_plan(core, tmp_path, 3001, http_port=port, live=live)
            from tests.test_live_text_window import live_clients
            from tests.test_onebot_delivery import core_client

            async with (
                core_client(core, bridge_config=cfg) as client,
                network.private_http_client(cfg.onebot_http_url, cfg.onebot_token) as protocol,
                live_clients(core, cfg) if live else nullcontext(),
            ):
                with Spool(cfg) as spool:
                    window = TestWindow(spool, plan, clock=lambda: core.clock.now)
                    await window.open(client)
                    approval = await stage(window, client, core)

                    async def replace_stream(request):
                        if request.url.path == "/send_private_msg":
                            if change == "transfer_encoding":
                                request.headers["Transfer-Encoding"] = "chunked"
                                return
                            if change == "unbounded_stream":

                                class UnknownStream(httpx.AsyncByteStream):
                                    async def __aiter__(self):
                                        pytest.fail("unknown request stream must not be consumed")
                                        yield b"unreachable"

                                request.stream = UnknownStream()
                                return
                            body = json.loads(request.content)
                            body["message"][0]["data"]["text"] = "synthetic unapproved wire stream"
                            wire = json.dumps(body).encode()
                            request.stream = httpx.ByteStream(wire)
                            request.headers["Content-Length"] = str(len(wire))

                    protocol.event_hooks["request"].append(replace_stream)
                    result = await window.send_once(client, protocol, approval)
                    if change == "transfer_encoding":
                        assert sends == [] and result["attempt_states"] == {"UNKNOWN": 1}
                    else:
                        assert len(sends) == 1 and result["attempt_states"] == {"ACK": 1}
                        assert (
                            digest(sends[0]["message"][0]["data"]["text"])
                            == approval["reply_sha256"]
                        )

    run(scenario())


@pytest.mark.parametrize("live", [False])
@pytest.mark.parametrize("phase", ["capture", "send"])
def test_probe_wire_body_is_the_checked_empty_object(core, tmp_path, live, phase):
    received = []

    async def source(ws):
        await ws.send(json.dumps(raw_event()))
        await ws.wait_closed()

    async def scenario():
        async with source_window(core, tmp_path, source, live=live) as (
            window,
            client,
            protocol,
            _,
            sends,
        ):

            async def receive_protocol(scope, receive, send):
                body = bytearray()
                while True:
                    part = await receive()
                    body.extend(part.get("body", b""))
                    if not part.get("more_body"):
                        break
                received.append((scope["path"], bytes(body)))
                result = provider(
                    httpx.Request(
                        "POST",
                        "http://synthetic" + scope["path"],
                        content=bytes(body),
                    ),
                    sends,
                )
                await send(
                    {"type": "http.response.start", "status": result.status_code, "headers": []}
                )
                await send({"type": "http.response.body", "body": result.content})

            async def replace_probe_stream(request):
                if request.url.path != "/send_private_msg":
                    wire = b'{"unexpected":true}'
                    request.stream = httpx.ByteStream(wire)
                    request.headers["Content-Length"] = str(len(wire))

            protocol._transport = httpx.ASGITransport(app=receive_protocol)
            protocol.event_hooks["request"].append(replace_probe_stream)
            from tests.test_live_text_window import live_clients

            async with live_clients(core, window.plan.config) if live else nullcontext():
                if phase == "capture":
                    result = await window.capture_once(client, protocol)
                    assert result["new_inputs"] == 1
                else:
                    approval = await stage(window, client, core)
                    result = await window.send_once(client, protocol, approval)
                    assert result["attempt_states"] == {"ACK": 1}
            probes = [body for path, body in received if path != "/send_private_msg"]
            assert len(probes) == 3 and all(body == b"{}" for body in probes)

    run(scenario())
