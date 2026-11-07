"""Native cutover gates, historical receipts, real HTTP and independent processes."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from astrbot_plugin_companion_bridge.bridge import Spool as LegacySpool
from companion.contracts import InboundEvent
from companion_onebot_bridge import migrate_astrbot
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.astrbot_media_retirement import BOUNDARY, native_file, old_spool
from tests.support.content_bridge import (
    bridge_module,  # noqa: F401
    event,
)
from tests.support.content_bridge import config as plugin_config
from tests.support.core_http import real_core_http
from tests.support.onebot_events import config, raw_event

ROOT = Path(__file__).resolve().parents[1]
def bundle(tmp_path, *, pending=False, cfg=None):
    cfg = cfg or config(data_dir=str(tmp_path / "native"), delivery_enabled=True)
    path, plugin, key, payload = old_spool(tmp_path, cfg, pending=pending)
    target = tmp_path / "migration"
    value = migrate_astrbot.prepare(plugin, native_file(tmp_path / "input.toml", cfg),
                                    path, target, BOUNDARY)
    return target, path, plugin, value, key, payload


def test_retired_plugin_stops_owner_fallback_without_allocating_or_ingesting(
    bridge_module, monkeypatch,  # noqa: F811
):  # noqa: F811
    def forbidden(*args, **kwargs):
        pytest.fail("retired ingress must not allocate a queue, client or spool")

    monkeypatch.setattr(bridge_module, "Spool", forbidden)
    monkeypatch.setattr(bridge_module.httpx, "AsyncClient", forbidden)
    bridge = bridge_module.CompanionBridge(object(), plugin_config(content_enabled=True))
    run(bridge.initialize())
    order = []
    run(bridge.on_private_message(event(order, raw={"message": [{"type": "image", "data": {
        "url": "https://gchat.qpic.cn/old?rkey=never-read"}}]})))
    run(bridge.on_private_message(event(order, sender="99999")))
    assert order == ["stop"]
    assert bridge.client is bridge.spool is bridge.task is None
    assert not hasattr(bridge, "content_queue")
    run(bridge.terminate())


def test_reader_migration_still_validates_before_resources(bridge_module, monkeypatch):  # noqa: F811
    bridge = bridge_module.CompanionBridge(object(), plugin_config(bilibili_reader_enabled=True))
    with pytest.raises(ValueError, match="requires migration"):
        run(bridge.initialize())
    assert bridge.client is bridge.spool is bridge.task is None


def test_export_does_not_seal_or_activate_and_reports_unrecoverable_urls(tmp_path):
    target, path, _, value, _, _ = bundle(tmp_path, pending=True)
    cfg = BridgeConfig.load(target / "native.toml")
    assert not cfg.delivery_enabled and cfg.capture_signing_key is None
    assert cfg.ingress_after == BOUNDARY
    assert value["inventory"]["image_uploads"]["pending_slots"] == 1
    assert value["inventory"]["image_uploads"]["restartable_urls"] == 0
    assert value["capabilities"]["media"] == "requires_existing_native_capture_and_authority"
    assert not json.loads((target / "astrbot-config.json").read_text())["drain_existing"]
    with pytest.raises(ValueError, match="HANDOFF_REQUIRED"):
        Spool(cfg)
    with pytest.raises(ValueError, match="RECONCILIATION_REQUIRED"):
        migrate_astrbot.seal(target, host_stopped=True, now=BOUNDARY)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='native_handoff'").fetchone()
    if os.name != "nt":
        assert all(p.stat().st_mode & 0o077 == 0 for p in target.iterdir())


def test_sealed_identity_blocks_parallel_old_host_replay_and_removed_handoff(tmp_path):
    target, path, plugin, _, _, payload = bundle(tmp_path)
    with pytest.raises(ValueError, match="STOP_OLD_ASTRBOT"):
        migrate_astrbot.seal(target, host_stopped=False, now=BOUNDARY)
    migrate_astrbot.seal(target, host_stopped=True, now=BOUNDARY)
    # Idempotent local sealing is allowed after the boundary as well.
    migrate_astrbot.seal(target, host_stopped=True, now=BOUNDARY + 10)
    cfg = BridgeConfig.load(target / "native.toml")
    with Spool(cfg):
        result = subprocess.run([sys.executable, "-B", "-c", """
import sys
from astrbot_plugin_companion_bridge.bridge import Spool
try:
    Spool(sys.argv[1])
except OSError:
    sys.exit(0)
sys.exit(9)
""", str(path)], cwd=ROOT, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stderr
    legacy = migrate_astrbot.legacy_spool(path, json.loads(plugin.read_text()))
    try:
        with pytest.raises(ValueError, match="RETIRED"):
            legacy.put("replay", payload)
        with pytest.raises(ValueError, match="RETIRED"):
            run(legacy.flush_one(object()))
    finally:
        legacy.close()
    with pytest.raises(ValueError, match="BRIDGE_IDENTITY_CHANGED"):
        Spool(replace(cfg, astrbot_handoff_path=None, astrbot_handoff_sha256=None, ingress_after=0))
    with pytest.raises(ValueError, match="HANDOFF_CHANGED"):
        Spool(replace(cfg, ingress_after=BOUNDARY - 1))
    for timestamp in (BOUNDARY - 1, BOUNDARY, "bad"):
        assert normalize(json.dumps(raw_event(time=timestamp)), cfg) is None
    assert normalize(json.dumps(raw_event(time=BOUNDARY + 1)), cfg)
    assert normalize(json.dumps(raw_event(time=BOUNDARY + 1, user_id=99999)), cfg) is None


def test_bad_native_identity_and_reused_generation_do_not_export(tmp_path):
    cfg = config(data_dir=str(tmp_path / "native"))
    path, plugin, _, _ = old_spool(tmp_path, cfg)
    for changes, error in (({"owner_qq_id": "10003"}, "IDENTITY_CHANGED"),
                           ({"protocol_epoch": "old-generation"}, "FRESH_PROTOCOL")):
        source = native_file(tmp_path / "input.toml", replace(cfg, **changes))
        with pytest.raises(ValueError, match=error):
            migrate_astrbot.prepare(plugin, source, path, tmp_path / "export", BOUNDARY)
        assert not (tmp_path / "export").exists()


@pytest.mark.parametrize("mode", [
    "accept", "erased", "lost_ack", "result_unknown", "waiting_image",
])
def test_real_http_independent_migration_cli_reconciles_without_image_or_delivery_io(
    core, tmp_path, mode,
):
    vision = None
    if mode == "waiting_image":
        from tests.support.content import enable

        _, vision = enable(core)

    async def child(*args):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-m", "companion_onebot_bridge.migrate_astrbot", *map(str, args),
            cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), 30)
            assert process.returncode == 0, err.decode(errors="replace")
            return json.loads(out)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def scenario():
        async with real_core_http(core) as port:
            cfg = config(data_dir=str(tmp_path / "native"), core_url=f"http://127.0.0.1:{port}",
                         bridge_token=core.settings.bridge_token.get_secret_value())
            path, plugin, key, payload = old_spool(tmp_path, cfg, pending=True)
            if mode in {"erased", "lost_ack", "result_unknown"}:
                core.ingest(InboundEvent.model_validate(payload), key)
            if mode == "erased":
                with core.store.transaction():
                    core.ledger.expire_events(core.store, [payload["event_id"]], core.clock())
            if mode == "result_unknown":
                from companion.contracts import LeaseRequest
                from companion.transport import begin_send, lease_deliveries

                assert await core.run_once("old-completed")
                item = lease_deliveries(core.store, core.settings,
                                       LeaseRequest(worker_id="old"), core.clock())["items"][0]
                begin_send(core.store, item["message_id"], item["lease_token"], core.clock())
                spool = LegacySpool(str(path))
                spool.start_delivery(item)
                spool.close()
            first = await child("drain", "--plugin-config", plugin, "--spool", path)
            second = await child("drain", "--plugin-config", plugin, "--spool", path)
            assert first == second and first["inbox"].get("pending", 0) == 0
            with sqlite3.connect(path) as db:
                receipt = json.loads(db.execute("SELECT value FROM retired_inbox").fetchone()[0])
                assert receipt["event_id"] == payload["event_id"]
                assert receipt["state"] == ("erased" if mode == "erased" else "accepted")
                assert receipt["image_slots"] == [0]
                if mode == "result_unknown":
                    assert json.loads(db.execute("SELECT body FROM results").fetchone()[0])[
                        "result"] == "DELIVERY_UNCERTAIN"
            assert len(core.store.rows("SELECT * FROM events")) == 1
            if mode == "waiting_image":
                async with httpx.AsyncClient(base_url=cfg.core_url, trust_env=False,
                    headers={"Authorization": "Bearer " + cfg.bridge_token}) as client:
                    route = "/internal/v2/content/" + payload["event_id"]
                    status = await client.get(route)
                    assert status.json()["items"][0]["state"] == "WAITING"
                    core.clock.advance(core.content.options.wait_seconds + 1)
                    status = await client.get(route)
                    assert status.json()["items"][0]["state"] == "UNKNOWN"
                    assert not status.json()["pending"]
                    core.clock.advance(core.content.options.upload_window_seconds + 1)
                    retry = await client.post(route + "/image/claim",
                                              json={"request_key": "image:0"})
                    assert retry.status_code == 410
                    assert retry.json() == {"error": "CONTENT_UPLOAD_EXPIRED"}
                assert vision.calls == []
            if mode == "result_unknown":
                assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"
            else:
                assert core.model.calls == [] and core.store.rows("SELECT * FROM outbox") == []

    run(scenario())


def test_drain_pump_only_calls_existing_inbox_and_receipts(
    bridge_module, tmp_path, monkeypatch,  # noqa: F811
):  # noqa: F811
    cfg = config(data_dir=str(tmp_path / "native"))
    path, _, _, _ = old_spool(tmp_path, cfg)
    assert path.name == "inbox.sqlite3"
    requests = []
    original_client = httpx.AsyncClient

    def factory(**kwargs):
        def respond(request):
            requests.append(request.url.path)
            pytest.fail("empty retirement pump must never lease new work")
        return original_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(bridge_module.httpx, "AsyncClient", factory)

    async def scenario():
        bridge = bridge_module.CompanionBridge(object(), plugin_config(
            drain_existing=True, protocol_epoch="old-generation", bridge_token=cfg.bridge_token))
        await bridge.initialize()
        task, client = bridge.task, bridge.client
        await asyncio.sleep(0)
        await bridge.terminate()
        await bridge.terminate()
        assert task.done() and client.is_closed and requests == []
        assert bridge.task is bridge.spool is bridge.client is None
        # Termination releases both the SQLite connection and the OS lock.
        reopened = LegacySpool(str(path))
        reopened.close()
