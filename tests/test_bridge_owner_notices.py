import asyncio
from dataclasses import replace

import pytest

from companion_onebot_bridge import owner_notices
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool, encode
from tests.support.onebot_events import raw_event
from tests.test_bridge_owner_runtime import config as config
from tests.test_reading_followup import window


def frame(config, now):
    return encode({"post_type": "notice", "notice_type": "friend_recall",
        "time": int(now.timestamp()), "self_id": int(config.bot_qq_id),
        "user_id": int(config.owner_qq_id), "message_id": 1})


def test_notice_journal_detects_projection_change(config):
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        now = runtime.clock()
        assert owner_notices.put(runtime, frame(config, now), now, ("127.0.0.1", 1))
        assert not owner_notices.put(runtime, frame(config, now), now, ("127.0.0.1", 1))
        runtime.verify()
        spool.db.execute("DELETE FROM automatic_recalls")
        with pytest.raises(ValueError, match="OWNER_RECALL_PROJECTION_CHANGED"):
            runtime.verify()


@pytest.mark.parametrize("early", [True, False])
def test_native_recall_queue_reaches_real_api_and_erases_original_input(
    core, tmp_path, monkeypatch, early,
):
    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, base, plan):
            cfg = replace(base, owner_authority=plan.authority,
                capture_signing_key=plan.key.private_bytes_raw().hex(),
                data_dir=str(tmp_path / "native-bridge"))
            with Spool(cfg) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                raw = encode(raw_event(1, time=int(core.clock.now.timestamp()), text="随后撤回"))
                event = normalize(raw, cfg, now=core.clock.now.timestamp())
                runtime.persist_capture(event, raw, received_at=core.clock.now,
                                        peer=("127.0.0.1", 1))
                if not early:
                    assert await runtime.flush_one(client)
                assert owner_notices.put(runtime, frame(cfg, core.clock.now), core.clock.now,
                                         ("127.0.0.1", 1))
                if early:
                    assert await runtime.flush_one(client)
                else:
                    runtime.stop()
                    assert await owner_notices.flush_one(runtime, client)
                runtime.verify(active=False)
                stored = core.store.one("SELECT redacted FROM events WHERE event_id=?",
                                        (event["event_id"],))
                if early:
                    assert stored is None
                    assert spool.db.execute("SELECT state,body FROM inbox").fetchone() == (
                        "erased", None)
                    assert not core.store.one("SELECT 1 FROM jobs")
                else:
                    assert stored == {"redacted": 1}
                assert spool.db.execute("SELECT state FROM automatic_recalls").fetchone()[
                    0] == "ACCEPTED"
                assert not await owner_notices.flush_one(runtime, client)
    asyncio.run(scenario())
