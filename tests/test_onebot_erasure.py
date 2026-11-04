import httpx
import pytest
from cryptography.fernet import Fernet

from astrbot_plugin_companion_bridge.bridge import Spool as LegacySpool
from astrbot_plugin_companion_bridge.bridge import normalize as legacy_normalize
from companion.backup import backup, restore
from companion.contracts import InboundEvent
from companion.core import Core
from companion_onebot_bridge.spool import Spool
from tests.conftest import FakeModel, run
from tests.support.onebot_events import config, normalized, raw_event
from tests.test_onebot_delivery import core_client
from tests.test_protocol_backends import astrbot_event


@pytest.mark.parametrize("body", [
    b'{"error":"OTHER","error":"EVENT_ERASED"}',
    b'{"error":"EVENT_ERASED","extra":true}',
    b'{"error":"OTHER"}',
    b'not json',
])
def test_legacy_spool_preserves_non_exact_erasure_receipts(tmp_path, body):
    spool = LegacySpool(str(tmp_path / "legacy.db"))
    try:
        spool.put(*legacy_normalize(astrbot_event(), "10001", "10002"))

        async def scenario():
            async with httpx.AsyncClient(
                base_url="http://core",
                transport=httpx.MockTransport(lambda r: httpx.Response(410, content=body)),
            ) as http:
                with pytest.raises((httpx.HTTPStatusError, ValueError)):
                    await spool.flush_one(http)

        run(scenario())
        pending = spool.db.execute("SELECT COUNT(*) FROM inbox WHERE state='pending'").fetchone()[0]
        assert pending == 1
    finally:
        spool.close()


def test_pre_event_backup_latest_ledger_blocks_pending_spool_resurrection(core, tmp_path):
    key = Fernet.generate_key()
    old_backup = tmp_path / "before-event.enc"
    backup(core.settings.database, old_backup, key)
    payload = normalized()
    cfg = config(data_dir=str(tmp_path / "bridge"))
    # Simulate committed Core accept with lost HTTP acknowledgment: Spool remains pending.
    with Spool(cfg) as spool:
        spool.put(payload)
        core.ingest(InboundEvent.model_validate(payload), payload["event_id"])
        with core.store.transaction():
            core.ledger.expire_events(core.store, [payload["event_id"]], core.clock())
    restored_path = tmp_path / "restored.db"
    restore(old_backup, restored_path, key, core.settings.erasure_ledger)
    settings = core.settings.model_copy(update={"database": str(restored_path)})
    restored = Core(settings, FakeModel(), core.clock)
    try:

        async def replay():
            with Spool(cfg) as spool:
                async with core_client(restored) as http:
                    await spool.flush_one(http)
                    assert spool.pending_count == 0
                    assert not await spool.flush_one(http)
                    assert not spool.put(payload)

        run(replay())
        assert restored.store.rows("SELECT * FROM events") == []
        assert not run(restored.run_once("offline-restore"))
        # The tombstone must not prevent an unrelated new owner event.
        other = normalized(raw_event(99, "new event"))
        restored.ingest(InboundEvent.model_validate(other), other["event_id"])
        assert run(restored.run_once("offline-restore"))
        assert len(restored.store.rows("SELECT * FROM outbox")) == 1
    finally:
        restored.store.close()


def test_complete_ledger_records_rebuild_missing_tombstones(core):
    payload = normalized()
    core.ingest(InboundEvent.model_validate(payload), payload["event_id"])
    with core.store.transaction():
        core.ledger.expire_events(core.store, [payload["event_id"]], core.clock())
    # Emulate a snapshot from the previous release with COMPLETE but no projection.
    core.store.execute("DELETE FROM runtime_meta WHERE key LIKE 'erased_event:%'")
    core.ledger.replay(core.store)
    import pytest

    from companion.contracts import CoreError

    with pytest.raises(CoreError, match="EVENT_ERASED"):
        core.ingest(InboundEvent.model_validate(payload), payload["event_id"])


def test_legacy_astrbot_spool_consumes_exact_erasure_receipt_and_keeps_chat_moving(core, tmp_path):
    spool = LegacySpool(str(tmp_path / "legacy.db"))
    try:
        key, payload = legacy_normalize(astrbot_event(), "10001", "10002")
        spool.put(key, payload)
        core.ingest(InboundEvent.model_validate(payload), key)
        with core.store.transaction():
            core.ledger.expire_events(core.store, [payload["event_id"]], core.clock())

        async def scenario():
            async with core_client(core) as http:
                await spool.flush_one(http)
                assert spool.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
                spool.put(*legacy_normalize(astrbot_event(message_id="43"), "10001", "10002"))
                await spool.flush_one(http)

        run(scenario())
        assert run(core.run_once("legacy-after-forget"))
        assert core.store.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 1
    finally:
        spool.close()
