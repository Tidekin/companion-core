"""Real native ingress, independent chat worker and final checked image delivery."""

import asyncio
import base64
import json
import sys
from dataclasses import replace

import httpx
import pytest

from companion import delivery_reconciliation, owner_authority
from companion.owner_authority_protocol import AuthorityStop
from companion_onebot_bridge import owner_native_resolution
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from companion_onebot_bridge.spool import Spool, encode
from tests.conftest import run
from tests.meme_authority_fixture import environment
from tests.support.onebot_events import raw_event
from tests.test_evaluation_process import process_config
from tests.test_next_memes_catalog import metadata, png


async def worker(core, tmp_path, candidate):
    config, output = tmp_path / "worker.json", tmp_path / "result.json"
    process_config(core, config)
    child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
        "tests.native_meme_host_process", str(config), core.clock(), candidate, str(output),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(40):
            _, error = await child.communicate()
        assert child.returncode == 0, error.decode()
        report = json.loads(output.read_text("utf-8"))
        assert report["worked"] and len(report["calls"]) == 1, report
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


def capture(owner, number=1):
    frame = encode(raw_event(number, time=int(owner.clock().timestamp()), text="太棒了，成功了"))
    event = normalize(frame, owner.config, now=owner.clock().timestamp())
    owner.persist_capture(event, frame, received_at=owner.clock(), peer=("127.0.0.1", 3001))
    return event


@pytest.mark.parametrize("outcome", ["ack", "unknown_sent", "unknown_cancelled",
                                    "stop", "mutate", "owner"])
def test_native_image_host_original_receipts(core, tmp_path, monkeypatch, outcome):
    core.settings.expression_selection_probability = 1.0
    sent = []

    async def scenario():
        async with environment(core, tmp_path, monkeypatch) as (base, cfg, grant, now):
            cfg = replace(cfg, core_url=str(base.base_url).rstrip("/"),
                onebot_http_url="http://127.0.0.1:3000", onebot_ws_url="ws://127.0.0.1:3001",
                capture_signing_key=grant.key.private_bytes_raw().hex(),
                owner_authority=grant.authority, data_dir=str(tmp_path / "host"),
                meme_enabled=True)
            with Spool(cfg) as spool:
                owner = OwnerRuntime(spool, clock=lambda: now)
                candidate = owner.memes.catalog.add(png(), metadata(), check=owner.verify)
                event = capture(owner)
                async with private_http_client(cfg.core_url, cfg.bridge_token) as client:
                    assert await owner.flush_one(client)
                    await worker(core, tmp_path, candidate)
                    content = json.loads(core.store.one("SELECT content_json FROM outbox")[
                        "content_json"])
                    assert content["image"]["candidate_id"] == candidate
                    guard = OwnerSendGuard(owner, client)

                    def provider(request):
                        if request.url.path == "/send_private_msg":
                            sent.append(json.loads(request.content))
                            if outcome.startswith("unknown"):
                                raise httpx.ReadTimeout("synthetic ambiguity", request=request)
                            data = {"message_id": 12345}
                        else:
                            data = {"/get_login_info": {"user_id": int(cfg.bot_qq_id)},
                                "/get_status": {"online": True, "good": True},
                                "/get_version_info": {"app_name": "SnowLuma",
                                    "app_version": "2.0.3-node"}}[request.url.path]
                        return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                                        "data": data})

                    async def hook(request):
                        if request.url.path != "/send_private_msg":
                            return
                        if outcome == "stop":
                            owner_authority.stop(core.store, core.settings, core.clock,
                                AuthorityStop(operation_id="stop-meme-host",
                                              authority=grant.authority))
                        elif outcome == "mutate":
                            guard.prepared_message[0]["data"]["text"] = "changed"
                            request._content = encode({"user_id": int(cfg.owner_qq_id),
                                "message": guard.prepared_message, "auto_escape": True}).encode()
                        elif outcome == "owner":
                            capture(owner, 2)

                    async with private_http_client(cfg.onebot_http_url, cfg.onebot_token,
                        transport=httpx.MockTransport(provider)) as protocol:
                        protocol.event_hooks["request"].append(hook)
                        assert await deliver_tick(client, OwnerOneBot(guard, protocol), spool,
                                                  guard=guard)
                    expected = "ACK" if outcome == "ack" else (
                        "NOT_SENT" if outcome == "owner" else "UNKNOWN")
                    assert spool.db.execute("SELECT state FROM window_attempts").fetchone()[0] == (
                        expected)
                    assert len(sent) == (1 if outcome == "ack" or outcome.startswith("unknown")
                                         else 0)
                    if sent:
                        assert sent[0]["message"][1]["data"]["file"] == (
                            "base64://" + base64.b64encode(png()).decode("ascii"))
                    assert owner.memes.catalog.db.execute(
                        "SELECT state FROM meme_pins").fetchone()[0] == (
                            "SENT" if outcome == "ack" else
                            "UNKNOWN" if outcome.startswith("unknown") else
                            "CANCELLED" if outcome == "owner" else "PREPARED")
                    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
                    assert core.store.one("SELECT origin_event_id FROM outbox")[
                        "origin_event_id"] == event["event_id"]
                    owner.verify(active=expected != "UNKNOWN")
                    if expected == "UNKNOWN":
                        assert owner.row()["state"] == "STOPPED"
                        with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                            owner.memes._owner_check()
            if outcome.startswith("unknown"):
                owner_authority.stop(core.store, core.settings, core.clock,
                    AuthorityStop(operation_id="stop-uncertain-meme", authority=grant.authority))
                core.clock.advance(60)
                original_usage = core.store.rows("SELECT * FROM model_usage")
                message_id = core.store.one("SELECT message_id FROM outbox")["message_id"]
                with Spool(cfg, reconciliation_only=True) as spool:
                    owner = OwnerRuntime(spool, clock=lambda: core.clock.now)
                    assert owner.row()["state"] == "STOPPED"
                    with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                        owner.memes._owner_check()
                    async with private_http_client(cfg.core_url, cfg.bridge_token) as client:
                        assert (await owner_native_resolution.prepare(owner, client, message_id))[
                            "state"] == "READY"
                        target = outcome.removeprefix("unknown_").upper()
                        resolved = delivery_reconciliation.resolve(core.store, core.settings,
                            message_id, target, core.clock(), operation_id="meme-host-resolution",
                            operation_sha256="e" * 64)
                        assert resolved["receipt"]["quiescence"]["authority"] == grant.authority
                        assert (await owner_native_resolution.reconcile(owner, client, message_id))[
                            "outcome"] == target
                    owner.verify(active=False)
                    assert spool.db.execute("SELECT state FROM meme_pins").fetchone()[0] == (
                        "UNKNOWN")
                    assert spool.db.execute("SELECT state FROM window_attempts").fetchone()[0] == (
                        "UNKNOWN")
                assert len(sent) == 1
                assert core.store.rows("SELECT * FROM model_usage") == original_usage
    run(scenario())
