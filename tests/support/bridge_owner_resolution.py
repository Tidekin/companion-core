import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta

from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerSendGuard
from companion_onebot_bridge.spool import Spool


async def child(module, *args):
    process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m", module, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(30):
            output, error = await process.communicate()
            assert process.returncode == 0, error.decode()
            return json.loads(output)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def reserve_uncertain(config):
    now = datetime.now(UTC)
    item = {"authority": config.owner_authority, "message_id": "msg_" + "a" * 32,
        "lease_token": "lease_" + "b" * 32, "lease_until": (now + timedelta(minutes=2)).isoformat(),
        "origin_event_id": "evt_" + "c" * 64, "action_type": "qq.private.reply",
        "content": {"text": "synthetic unknown"}, "reply_revision": 1}
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        guard = OwnerSendGuard(runtime, None)
        guard.reserve(item)
        spool.start_delivery(item)
        guard.complete(item, None)
    return item
