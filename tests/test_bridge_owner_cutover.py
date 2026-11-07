"""Explicit cutover retains verified v1 custody and never imports it as v2 work."""

import asyncio
from dataclasses import replace

import pytest

from companion import paid_budget
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from companion_onebot_bridge import journal, owner_cutover
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool, encode
from tests.support.onebot_capture import source_window
from tests.support.onebot_events import raw_event
from tests.support.onebot_pokes import silent
from tests.support.owner_authority import activate


@pytest.mark.parametrize("pending", [None, "unknown", "recall", "media"])
def test_verified_legacy_cutover_keeps_old_journal_and_rejects_unclosed_work(
    core, tmp_path, pending,
):
    async def run():
        async with source_window(core, tmp_path, silent, live=True, approved=True) as (
            old, client, _, _, _,
        ):
            await old.stop_live(client)
            config = old.plan.config
            original_head = old.row()["journal_head"]
            original_file = old.plan.journal.read_bytes()
            original_plan = old.row()["plan_json"]
            if pending == "unknown":
                old.db.execute("INSERT INTO window_attempts VALUES(?,?,?,?,?,?,?,?,?)",
                    ("msg_" + "a" * 32, "evt_" + "b" * 64, 1, "UNKNOWN", "c" * 64,
                     "d" * 64, None, "e" * 64, old.plan.sha256))
                old.db.commit()
            elif pending in {"recall", "media"}:
                table = "automatic_recalls" if pending == "recall" else "automatic_media"
                # A pending original queue is a blocker even before its evidence can be reconciled.
                old.db.execute(f"CREATE TABLE {table}(state TEXT)")
                old.db.execute(f"INSERT INTO {table} VALUES('PENDING')")
                old.db.commit()
        core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
        policy = OwnerGrantPolicy(owner_qq_id=core.settings.owner_qq_id, purposes=["chat"],
            authorization_sha256="a" * 64,
            paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
            capture={"public_key": config.capture_public_key,
                "identity_sha256": journal.digest([config.owner_qq_id, config.bot_qq_id]),
                "protocol_epoch": config.protocol_epoch})
        activated, _ = activate(core, policy)
        native_config = replace(config, owner_authority=activated["authority"])
        with Spool(native_config, reconciliation_only=True) as spool:
            if pending:
                with pytest.raises((ValueError, KeyError, IndexError)):
                    owner_cutover.inspect(spool)
                assert not spool.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='owner_runtime'").fetchone()
                return
            snapshot = owner_cutover.inspect(spool)
            runtime = OwnerRuntime(spool, clock=lambda: core.clock.now, legacy=snapshot)
            runtime.verify(active=False)
            saved = spool.db.execute("SELECT journal_head,plan_json FROM test_window").fetchone()
            assert saved[0] == original_head and saved[1] == original_plan
            assert old.plan.journal.read_bytes() == original_file
        with Spool(native_config) as spool:
            runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
            runtime.verify()
            frame = encode(raw_event(7, time=int(core.clock.now.timestamp()), text="new native"))
            event = normalize(frame, native_config, now=core.clock.now.timestamp())
            runtime.persist_capture(event, frame, received_at=core.clock.now, peer=("127.0.0.1", 1))
            runtime.verify()
            assert old.plan.journal.read_bytes() == original_file
            spool.db.execute("UPDATE test_window SET reason='changed'")
            with pytest.raises(ValueError, match="OWNER_LEGACY_CUSTODY_CHANGED"):
                runtime.verify()
    asyncio.run(run())
