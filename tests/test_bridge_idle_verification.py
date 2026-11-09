"""The real idle delivery loop validates custody without copying unused history."""

import asyncio

import httpx
import pytest

from companion_onebot_bridge import app, journal, owner_runtime
from companion_onebot_bridge.metrics import Metrics
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.support.bridge_owner_runtime import config as config


@pytest.mark.parametrize("damage", [None, "stop", "head"])
def test_idle_delivery_checks_live_custody_without_materializing_records(
    config, monkeypatch, damage,
):
    async def scenario():
        with Spool(config) as spool:
            runtime = OwnerRuntime(spool)
            runtime.verify(include_records=False)
            if damage == "stop":
                journal.stop_marker(runtime.stop_path)
            elif damage == "head":
                with runtime.db:
                    runtime.db.execute("UPDATE owner_runtime SET journal_head=?", ("f" * 64,))

            def copy_unused_history(*args):
                pytest.fail("idle delivery copied the complete owner journal")

            monkeypatch.setattr(owner_runtime, "deepcopy", copy_unused_history)
            requests = []

            def core(request):
                requests.append(request.url.path)
                if request.url.path == "/internal/v1/deliveries/lease":
                    return httpx.Response(200, json={"items": []})
                assert request.url.path == "/internal/v1/owner-authority/check"
                return httpx.Response(200, json={"approved": True,
                    "authority": runtime.authority, "purpose": "chat", "expires_at": None,
                    "budget_journal_id": "budget_" + "a" * 32, "capability_policies": {},
                    "capture": {"public_key": config.capture_public_key,
                        "identity_sha256": journal.digest([config.owner_qq_id, config.bot_qq_id]),
                        "protocol_epoch": config.protocol_epoch}})

            def onebot(request):
                pytest.fail("idle delivery contacted OneBot")

            stop = asyncio.Event()

            async def pause(event, seconds):
                assert event is stop
                stop.set()

            monkeypatch.setattr(app, "pause", pause)
            async with (
                httpx.AsyncClient(base_url=config.core_url,
                    transport=httpx.MockTransport(core)) as client,
                httpx.AsyncClient(base_url=config.onebot_http_url,
                    headers={"Authorization": "Bearer " + config.onebot_token},
                    transport=httpx.MockTransport(onebot)) as protocol,
            ):
                delivery = app.deliver(client, None, spool, stop, Metrics(),
                                       runtime=runtime, protocol=protocol)
                if damage:
                    code = ("OWNER_RUNTIME_STOPPED" if damage == "stop"
                            else "OWNER_JOURNAL_RECONCILIATION_REQUIRED")
                    with pytest.raises(ValueError, match=code):
                        await delivery
                    assert requests == []
                else:
                    await delivery
                    assert requests == ["/internal/v1/deliveries/lease",
                                        "/internal/v1/owner-authority/check"]

    asyncio.run(scenario())
