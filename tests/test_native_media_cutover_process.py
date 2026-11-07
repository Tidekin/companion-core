"""Migrated native capture through real Core HTTP, then independent media/chat workers."""

import asyncio
import base64
import json
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from companion.model import ModelClient
from companion_onebot_bridge import migrate_astrbot
from companion_onebot_bridge.capture import prepare_owner_input
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.onebot_client import OneBotClient
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.live_image_input import png
from tests.support.native_context import Capture, configure
from tests.support.onebot import provider
from tests.support.onebot_events import config, raw_event
from tests.support.onebot_images import image_segment
from tests.support.owner_recall_bridge_media import scope
from tests.test_astrbot_media_retirement import BOUNDARY, native_file, old_spool

ROOT = Path(__file__).resolve().parents[1]


async def child(packet):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-B", "-m", "tests.test_native_media_cutover_process", "--worker",
        cwd=ROOT, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(json.dumps(
            packet, default=lambda value: value.get_secret_value() if isinstance(value, SecretStr)
            else value.isoformat()).encode()), 35)
        assert process.returncode == 0, err.decode(errors="replace")
        return json.loads(out)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("mode", [
    "complete", "upload_receipt_lost", "erase_before_download", "old_frame",
])
def test_migrated_native_media_real_http_and_independent_workers(core, tmp_path, mode):
    calls, sends = [], []

    async def source(ws):
        if mode == "old_frame":
            await ws.send(json.dumps(raw_event(41, time=BOUNDARY)))
        else:
            await ws.send(json.dumps(raw_event(42, time=BOUNDARY + 1, message=[
                {"type": "text", "data": {"text": "请看这张图"}}, image_segment(),
            ])))
        await ws.wait_closed()

    def response(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "这是蓝色的图。"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    async def scenario():
        async with real_core_http(core) as port, serve(source, "127.0.0.1", 0) as server:
            cfg = config(data_dir=str(tmp_path / "bridge"),
                         core_url=f"http://127.0.0.1:{port}",
                         onebot_ws_url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                         onebot_http_url="http://127.0.0.1:3000",
                         capture_signing_key="6" * 64, rich_input_scope="references_v1")
            path, plugin, _, _ = old_spool(tmp_path, cfg)
            folder = tmp_path / "handoff"
            migrate_astrbot.prepare(plugin, native_file(tmp_path / "native.toml", cfg),
                                    path, folder, BOUNDARY)
            migrate_astrbot.seal(folder, host_stopped=True, now=BOUNDARY)
            migrated = BridgeConfig.load(folder / "native.toml")
            core.clock.now = datetime.fromtimestamp(BOUNDARY + 1, UTC)
            options = scope(core)
            for name, value in options["core_options"].items():
                setattr(core.settings, name, value)
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            from companion import paid_budget

            key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(migrated.capture_signing_key))
            grant = configure(core, tmp_path, media=True, owner_policy={
                "purposes": ["chat", "image", "file", "content"], "capture": {
                "public_key": key.public_key().public_bytes_raw().hex(),
                "identity_sha256": paid_budget.sha([cfg.owner_qq_id, cfg.bot_qq_id]),
                "protocol_epoch": migrated.protocol_epoch}})
            cfg = replace(migrated, owner_authority=grant.authority,
                          bridge_token=core.settings.bridge_token.get_secret_value())
            async with private_http_client(cfg.core_url, cfg.bridge_token) as client, (
                private_http_client(cfg.onebot_http_url, cfg.onebot_token,
                    transport=httpx.MockTransport(lambda request: provider(request, sends)))
            ) as protocol, httpx.AsyncClient(transport=httpx.MockTransport(response)) as model:
                core.model = ModelClient(core.settings, model)
                core.content.options = core.settings.content
                with Spool(cfg) as spool:
                    runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                    window = Capture(core, spool, runtime)
                    if mode == "old_frame":
                        async with connect(cfg.onebot_ws_url, proxy=None, additional_headers={
                            "Authorization": "Bearer " + cfg.onebot_token}) as ws:
                            prepared = await prepare_owner_input(runtime, client,
                                OneBotClient(cfg, protocol), await ws.recv(), core.clock.now,
                                ws.remote_address)
                        assert prepared is None
                        assert not core.store.rows("SELECT * FROM events")
                        assert calls == sends == []
                        return
                    await window.capture_once(client, protocol)
                    assert await runtime.flush_one(client)
                    event_id = spool.db.execute(
                        "SELECT event_id FROM automatic_media").fetchone()[0]
                    assert core.store.one("SELECT COUNT(*) n FROM events")["n"] == 1
                if mode == "erase_before_download":
                    with core.store.transaction():
                        core.ledger.expire_events(core.store, [event_id], core.clock())
                packet = {"config": asdict(cfg), "clock": core.clock(),
                          "settings": core.settings.model_dump(mode="python"), "mode": mode,
                          "operation": "media"}
                first = await child(packet)
                second = await child({**packet, "mode": "complete"})
                if mode == "erase_before_download":
                    assert first["downloads"] == second["downloads"] == 0
                    assert calls == [] and not core.store.rows("SELECT * FROM model_usage")
                    assert not (await child({**packet, "operation": "chat"}))["worked"]
                else:
                    assert first["downloads"] == 1 and second["downloads"] == 0, (first, second)
                    assert first["state"] == (
                        "UPLOADING" if mode == "upload_receipt_lost" else "DONE")
                    assert second["state"] == "DONE"
                    assert not calls  # Upload does not dispatch a model request.
                    assert (await child({**packet, "operation": "chat"}))["worked"]
                    assert not (await child({**packet, "operation": "chat"}))["worked"]
                    with Spool(cfg) as spool:
                        runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                        sent = await Capture(core, spool, runtime).send(client, protocol)
                        assert sent["worked"]
                    assert len(sends) == 1
                    assert core.store.one("SELECT state FROM outbox")["state"] == "SENT"
                    assert core.store.one(
                        "SELECT COUNT(*) n FROM model_usage WHERE state='SETTLED'")["n"] == 1
                assert not core.store.one("SELECT 1 FROM model_usage WHERE state='RESERVED'")

    run(scenario())


async def worker(packet):
    from companion.config import Settings
    from companion.core import Core
    from companion_onebot_bridge import pending_media

    if packet["operation"] == "chat":
        settings = Settings.model_validate(packet["settings"])

        def respond(request):
            assert base64.b64encode(png()).decode() in request.content.decode()
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "我看到蓝色的图了。"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as model:
            with Core(settings, ModelClient(settings, model), lambda: packet["clock"]) as core:
                return {"worked": await core.run_once("independent-cutover-chat")}
    cfg = BridgeConfig(**packet["config"])
    downloads = []

    def download(request):
        downloads.append(True)
        return httpx.Response(200, content=png(), headers={"Content-Type": "image/png"})

    async def resolve(*args):
        return ["1.1.1.1"]

    transition = pending_media.transition

    def interrupted(window, event_id, state, slot, receipt=None):
        if state == "DONE":
            raise RuntimeError("synthetic interruption after Core acceptance")
        return transition(window, event_id, state, slot, receipt)

    if packet["mode"] == "upload_receipt_lost":
        pending_media.transition = interrupted
    with Spool(cfg) as spool:
        window = OwnerRuntime(spool, clock=lambda: datetime.fromisoformat(packet["clock"]))
        row = pending_media._execute(window, "SELECT * FROM automatic_media").fetchone()
        async with private_http_client(cfg.core_url, cfg.bridge_token) as client:
            pump = pending_media.MediaPump(window, client,
                image_transport=httpx.MockTransport(download), image_resolver=resolve)
            error = None
            if row["state"] not in pending_media.TERMINAL:
                try:
                    await pump.execute(row)
                except (ValueError, httpx.HTTPError, RuntimeError) as exc:
                    error = str(exc)
            state = spool.db.execute("SELECT state FROM automatic_media").fetchone()[0]
            return {"downloads": len(downloads), "state": state, "error": error}


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    print(json.dumps(run(worker(json.loads(sys.stdin.buffer.read())))))
