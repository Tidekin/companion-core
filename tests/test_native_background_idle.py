"""Idle background tasks wait without custody replay; actual media work stays fenced."""

import asyncio
import time

import httpx
import pytest

from companion_onebot_bridge import app, journal, pending_media
from companion_onebot_bridge.owner_memes import MemePump
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.profile_probe import ProfilePump
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.bridge_owner_runtime import config as config
from tests.support.onebot_images import image_segment
from tests.test_native_image_bursts import native_host, send


def test_idle_background_waits_skip_history_and_stop_wakes_profile_cooldown(config, monkeypatch):
    async def scenario():
        with Spool(config) as spool:
            runtime = OwnerRuntime(spool)
            profile = ProfilePump(runtime, None, None)
            media = pending_media.MediaPump(runtime, None)
            memes = MemePump(runtime, None)
            profile.next_run = time.monotonic() + 60
            stop, paused = asyncio.Event(), asyncio.Event()
            original_verify, original_pause = runtime.verify, app.pause
            checks, pauses = [], []

            def verify(**kwargs):
                checks.append(kwargs)
                return original_verify(**kwargs)

            async def pause(event, seconds):
                pauses.append((asyncio.current_task().get_name(), seconds))
                if len({name for name, _ in pauses}) == 2:
                    paused.set()
                await original_pause(event, seconds)

            monkeypatch.setattr(runtime, "verify", verify)
            monkeypatch.setattr(app, "pause", pause)
            tasks = [asyncio.create_task(app.observe_profile(profile, stop, memes), name="profile"),
                     asyncio.create_task(app.transfer_media(media, stop), name="media")]
            try:
                await asyncio.wait_for(paused.wait(), 2)
                await asyncio.sleep(0.15)
            finally:
                stop.set()
                await asyncio.wait_for(asyncio.gather(*tasks), 2)
            assert checks == [], {"checks": len(checks), "pauses": pauses}
            profile_waits = [seconds for name, seconds in pauses if name == "profile"]
            assert len(profile_waits) == 1 and profile_waits[0] > 50
            assert not profile.running and media.task is None

    run(scenario())


@pytest.mark.parametrize("change", ["stop", "journal"])
def test_real_media_work_rechecks_custody_before_core_or_cdn(core, tmp_path, monkeypatch, change):
    async def scenario():
        async with native_host(core, tmp_path, monkeypatch, count=1) as values:
            active, inbound, _, sends, _ = values
            await send(core, inbound, 1, [image_segment("https://gchat.qpic.cn/guard", subtype=1)])
            await active.capture()
            await active.flush()
            assert active.db.execute("SELECT state FROM automatic_media").fetchone() == ("PENDING",)
            if change == "stop":
                journal.stop_marker(active.runtime.stop_path)
            else:
                active.runtime.path.write_bytes(active.runtime.path.read_bytes() + b"truncated")
            requests = []

            async def observe(request):
                requests.append(request.url.path)

            def cdn(request):
                pytest.fail("unverified custody reached CDN")

            active.client.event_hooks["request"].append(observe)
            pump = pending_media.MediaPump(active.runtime, active.client,
                                           image_transport=httpx.MockTransport(cdn))
            with pytest.raises(ValueError):
                await app.transfer_media(pump, asyncio.Event())
            assert requests == [] and sends == [] and pump.task is None
            assert core.store.one("SELECT state FROM content_items")["state"] == "WAITING"

    run(scenario())
