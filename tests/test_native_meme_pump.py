"""Native semantic host uses the admitted revision without holding owner input."""

import json
from dataclasses import replace

import httpx

from companion import meme_core
from companion_onebot_bridge.meme_runtime import MemeRuntime
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_memes import MemePump
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.meme_authority_fixture import environment
from tests.support.meme_runtime import RULE, public_transport
from tests.support.native_meme_host import capture, worker
from tests.support.next_memes_discovery import resolver


def test_native_pump_offers_current_revision_and_yields_to_pending_input(
    core, tmp_path, monkeypatch,
):
    core.settings.meme_semantics_enabled = True

    class Encoder:
        def query_embed(self, _):
            return [[1.0] + [0.0] * (meme_core.SPEC.dimensions - 1)]

    monkeypatch.setattr(meme_core, "encoder", lambda *args, **kwargs: Encoder())

    async def scenario():
        async with environment(core, tmp_path, monkeypatch) as (base, cfg, grant, now):
            cfg = replace(cfg, core_url=str(base.base_url).rstrip("/"),
                onebot_http_url="http://127.0.0.1:3000", onebot_ws_url="ws://127.0.0.1:3001",
                capture_signing_key=grant.key.private_bytes_raw().hex(),
                owner_authority=grant.authority, data_dir=str(tmp_path / "host"),
                meme_enabled=True, meme_semantics_enabled=True)
            with Spool(cfg) as spool:
                owner = OwnerRuntime(spool, clock=lambda: now)
                async with private_http_client(cfg.core_url, cfg.bridge_token) as client:
                    pump = MemePump(owner, client)
                    try:
                        event = capture(owner)
                        assert await owner.flush_one(client)
                        pump.admitted(event["event_id"])
                        assert pump.query_task is not None
                        await pump.query_task
                        saved = spool.db.execute("SELECT reply_revision,state FROM "
                            "meme_runtime_semantic_offers WHERE event_id=?", (event["event_id"],)
                        ).fetchone()
                        assert saved == (1, "ACCEPTED")
                        value = json.loads(core.store.one(
                            "SELECT value FROM runtime_meta WHERE key=?",
                            (meme_core.OFFER + event["event_id"],))["value"])
                        assert value["reply_revision"] == 1
                        assert value["offer"]["candidates"] == []
                        assert core.store.one("SELECT state FROM jobs WHERE kind='chat'")[
                            "state"] == "QUEUED"
                        second = capture(owner, 2)
                        pump.tick(idle=False)
                        assert pump.task is None
                        assert await owner.flush_one(client)
                        pump.admitted(second["event_id"])
                        await pump.query_task
                        assert spool.db.execute("SELECT reply_revision,state FROM "
                            "meme_runtime_semantic_offers WHERE event_id=?", (second["event_id"],)
                        ).fetchone() == (2, "ACCEPTED")
                        assert not core.store.rows("SELECT * FROM model_usage")
                        assert not core.store.rows("SELECT * FROM outbox")
                    finally:
                        await pump.close()
    run(scenario())


def test_native_idle_pump_discovers_only_after_original_reply(core, tmp_path, monkeypatch):
    core.settings.meme_discovery_enabled = True
    core.settings.meme_discovery_daily_queries = 2
    core.settings.meme_discovery_daily_download_bytes = 32 * 1024 * 1024
    core.settings.meme_discovery_daily_storage_bytes = 8 * 1024 * 1024
    calls = []

    async def scenario():
        async with environment(core, tmp_path, monkeypatch) as (base, cfg, grant, now):
            cfg = replace(cfg, core_url=str(base.base_url).rstrip("/"),
                onebot_http_url="http://127.0.0.1:3000", onebot_ws_url="ws://127.0.0.1:3001",
                capture_signing_key=grant.key.private_bytes_raw().hex(),
                owner_authority=grant.authority, data_dir=str(tmp_path / "host"),
                meme_enabled=True, meme_discovery_enabled=True, meme_trusted_sources=(RULE,))
            with Spool(cfg) as spool:
                owner = OwnerRuntime(spool, clock=lambda: now)
                owner._memes = MemeRuntime(owner, transport=public_transport(calls),
                                           resolver=resolver)
                capture(owner)
                async with private_http_client(cfg.core_url, cfg.bridge_token) as client:
                    await owner.flush_one(client)
                    pump = MemePump(owner, client)
                    try:
                        pump.tick(idle=True)
                        await pump.task
                        assert not calls
                        assert not core.store.rows("SELECT * FROM meme_discovery_reservations")
                        await worker(core, tmp_path, "-")

                        def provider(request):
                            data = {"/get_login_info": {"user_id": int(cfg.bot_qq_id)},
                                "/get_status": {"online": True, "good": True},
                                "/get_version_info": {"app_name": "SnowLuma",
                                    "app_version": "2.0.3-node"},
                                "/send_private_msg": {"message_id": 12345}}[request.url.path]
                            return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                                            "data": data})

                        async with private_http_client(cfg.onebot_http_url, cfg.onebot_token,
                            transport=httpx.MockTransport(provider)) as protocol:
                            guard = OwnerSendGuard(owner, client)
                            assert await deliver_tick(client, OwnerOneBot(guard, protocol), spool,
                                                      guard=guard)
                        pump.next_run = 0
                        pump.tick(idle=True)
                        await pump.task
                        assert calls
                        assert core.store.one(
                            "SELECT state FROM jobs WHERE kind='meme_discovery'") == {
                            "state": "SUCCEEDED"}
                        assert core.store.one(
                            "SELECT state,queries FROM meme_discovery_reservations") == {
                            "state": "SETTLED", "queries": 1}
                        before = len(calls)
                        capture(owner, 2)
                        pump.next_run = 0
                        pump.tick(idle=True)
                        assert len(calls) == before
                        assert not pump.task or pump.task.done()
                    finally:
                        await pump.close()
    run(scenario())
