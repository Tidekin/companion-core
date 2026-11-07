"""Creating a successor's notice queue must not invalidate sealed predecessor custody."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from companion_onebot_bridge import journal, owner_handoff, owner_notices, pending_media
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.support.bridge_owner_handoff import core_handler
from tests.support.bridge_owner_notices import frame
from tests.support.bridge_owner_runtime import config as config


@pytest.mark.parametrize("old_queues", [(), ("automatic_recalls",), ("automatic_media",),
                                       ("automatic_recalls", "automatic_media")])
def test_first_recall_after_handoff_keeps_original_custody(config, monkeypatch, old_queues):
    original = owner_handoff.closed
    old_digest = journal.digest({"inputs": [], "attempts": [], "results": [], "inbox": [],
                                 "queues": dict.fromkeys(old_queues, [])})

    def legacy_seal(*args, **kwargs):
        value = original(*args, **kwargs)
        return value if kwargs.get("expected") else old_digest

    # Keep actual historical seals, including each legacy empty-table representation.
    monkeypatch.setattr(owner_handoff, "closed", legacy_seal)
    with Spool(config) as spool:
        OwnerRuntime(spool).stop()
    successor = config.owner_authority | {"revision": 3, "policy_sha256": "e" * 64}
    with Spool(config, reconciliation_only=True) as spool:
        runtime = OwnerRuntime(spool, clock=lambda: datetime.now(UTC) + timedelta(seconds=1))

        async def handoff():
            async with httpx.AsyncClient(base_url="http://127.0.0.1:8765",
                transport=httpx.MockTransport(core_handler(config, successor))) as client:
                await owner_handoff.prepare(runtime, client)
                await owner_handoff.handoff(runtime, client, successor)
        asyncio.run(handoff())

    monkeypatch.setattr(owner_handoff, "closed", original)
    current = replace(config, owner_authority=successor)
    with Spool(current) as spool:
        runtime = OwnerRuntime(spool)
        prefix = runtime.verify()
        assert next(r["data"]["custody_sha256"] for r in prefix
                    if r["kind"] == "OWNER_HANDOFF_PREPARED") == old_digest
        now = runtime.clock()
        assert owner_notices.put(runtime, frame(current, now), now, ("127.0.0.1", 1))
        pending_media.prepare(runtime)
        assert runtime.verify()[:len(prefix)] == prefix
    with Spool(current) as spool:
        assert OwnerRuntime(spool).verify()[-1]["kind"] == "RECALL_RECEIVED"
