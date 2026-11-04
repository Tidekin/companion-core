"""Explicit native private companion purposes over the real continuous host."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from companion import private_life
from companion_onebot_bridge.pending_media import MediaPump
from tests.native_continuous_support import host
from tests.support.onebot_events import raw_event as platform_event


def raw_event(*args, **kwargs):
    return platform_event(*args, time=int(datetime.now(UTC).timestamp()), **kwargs)


@asynccontextmanager
async def companion_host(core, tmp_path, source, *, options, bridge_options=None,
                         rich_input_scope="none", independent_api=False):
    now = datetime.now(UTC)
    purposes = ["chat", "history", *private_life.FEATURES]
    if options["core_options"].get("content_enabled"):
        purposes.append("content")
    if options["paid_options"].get("image_input"):
        purposes.append("image")
    async with host(core, tmp_path, source,
        core_options={**options["core_options"], "rich_input_scope": rich_input_scope},
        paid_options={**options["paid_options"], "expires_at": now + timedelta(days=2),
                      "rates_valid_until": now + timedelta(days=2), "daily_calls": 100},
        purposes=purposes, grant_options={"expires_at": now + timedelta(days=2)},
        bridge_options={"rich_input_scope": rich_input_scope, **(bridge_options or {})},
        independent_api=independent_api,
    ) as (active, peers, sends, _):
        yield active, active.client, active.protocol, peers, sends


async def ingress(active, *, image_transport=None, image_resolver=None):
    await active.capture(active.captures() + 1)
    await active.flush()
    pump = MediaPump(active.runtime, active.client, protocol=active.onebot,
                     image_transport=image_transport, image_resolver=image_resolver)
    try:
        while True:
            pump.tick()
            if pump.task is None:
                break
            await pump.task
    finally:
        await pump.close()
