"""Real daemon scheduling preserves discovery reservations and urgent input fences."""

import asyncio
from dataclasses import replace

import httpx
import pytest

from companion_onebot_bridge import app, meme_runtime, profile_probe
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


@pytest.mark.parametrize("new_input", [False, True])
def test_profile_waits_for_reserved_discovery_but_owner_input_interrupts(
    core, tmp_path, monkeypatch, new_input,
):
    core.settings.meme_discovery_enabled = True
    calls = []

    async def scenario():
        ready, release = asyncio.Event(), asyncio.Event()
        profile_checked, profile_started, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = meme_runtime._Budget.check
        original_pause = app.pause

        async def collect(*args, **kwargs):
            profile_started.set()
            return {"status": "IDLE"}

        async def pause(event, seconds):
            if asyncio.current_task().get_name() == "profile":
                profile_checked.set()
            await original_pause(event, seconds)

        async def checked(reservation):
            await original(reservation)
            if not ready.is_set():
                ready.set()
                await release.wait()

        monkeypatch.setattr(meme_runtime._Budget, "check", checked)
        monkeypatch.setattr(profile_probe, "collect", collect)
        monkeypatch.setattr(app, "pause", pause)
        async with environment(core, tmp_path, monkeypatch) as (base, cfg, grant, now):
            cfg = replace(cfg, core_url=str(base.base_url).rstrip("/"),
                onebot_http_url="http://127.0.0.1:3000", onebot_ws_url="ws://127.0.0.1:3001",
                capture_signing_key=grant.key.private_bytes_raw().hex(),
                owner_authority=grant.authority, data_dir=str(tmp_path / "host"),
                meme_enabled=True, meme_discovery_enabled=True, meme_trusted_sources=(RULE,),
                poll_seconds=0.05)
            with Spool(cfg) as spool:
                owner = OwnerRuntime(spool, clock=lambda: now)
                owner._memes = MemeRuntime(owner, transport=public_transport(calls),
                                           resolver=resolver)
                capture(owner)
                async with private_http_client(cfg.core_url, cfg.bridge_token) as client:
                    await owner.flush_one(client)
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
                    pump = MemePump(owner, client)
                    profile = profile_probe.ProfilePump(owner, client, None)
                    tasks = [asyncio.create_task(app.discover_memes(pump, stop, profile))]
                    try:
                        async with asyncio.timeout(15):
                            await ready.wait()
                        assert not calls
                        discovery = pump.task
                        assert pump.running
                        tasks.append(asyncio.create_task(
                            app.observe_profile(profile, stop, pump), name="profile"))
                        await asyncio.wait_for(profile_checked.wait(), 5)
                        assert not profile.running and not profile_started.is_set()
                        assert not discovery.cancelling()
                        if new_input:
                            capture(owner, 2)
                            # The next daemon tick must cancel before public I/O.
                            await asyncio.wait_for(
                                asyncio.gather(discovery, return_exceptions=True), 5)
                        release.set()
                        await asyncio.wait_for(
                            asyncio.gather(discovery, return_exceptions=True), 15)
                        row = core.store.one("SELECT state,queries,requests,download_bytes,"
                            "reserved_download_bytes FROM meme_discovery_reservations")
                        if new_input:
                            assert row == {"state": "UNKNOWN", "queries": 0, "requests": 0,
                                "download_bytes": 0, "reserved_download_bytes": 16777216}
                            assert not calls
                        else:
                            assert row["state"] == "SETTLED" and row["queries"] == 1
                            assert calls
                            await asyncio.wait_for(profile_started.wait(), 5)
                        assert not pump.running
                    finally:
                        stop.set()
                        release.set()
                        await asyncio.wait_for(asyncio.gather(*tasks), 5)
    run(scenario())
