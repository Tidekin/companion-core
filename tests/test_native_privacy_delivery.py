"""An original reply cannot cross a privacy revision at the final HTTP boundary."""

import pytest

from companion.core import Core
from companion.privacy import advance_epoch
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerSendGuard
from tests.conftest import run
from tests.support.native_dispatch import dispatch, staged


@pytest.mark.parametrize("boundary", ["final_request", "restart"])
def test_frozen_reply_privacy_revision_blocks_without_rebilling(core, tmp_path, boundary):
    async def scenario():
        async with staged(core, tmp_path) as (owner, client, protocol, guard, sends):
            usage = core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence')

            def change():
                with Core(core.settings, clock=core.clock) as independent:
                    assert independent.store is not core.store
                    with independent.store.transaction(control_write=True):
                        advance_epoch(independent.store)

            if boundary == "restart":
                change()
                owner = OwnerRuntime(owner.spool, clock=lambda: core.clock.now)
                guard = OwnerSendGuard(owner, client)
            else:
                async def final_request(request):
                    if request.url.path == "/send_private_msg":
                        change()
                protocol.event_hooks["request"].append(final_request)
            await dispatch(owner, client, protocol, guard)
            assert not sends and not guard.dispatched
            # Once begin-send is durable, the original uncertain outcome remains
            # unless the existing cancellation protocol proves a safe rollback.
            expected = "CANCELLED" if boundary == "restart" else "DELIVERY_UNCERTAIN"
            assert core.store.one("SELECT state FROM outbox")["state"] == expected
            assert core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence') == usage
            owner.verify(active=False)
            assert owner.row()["state"] == (
                "ACTIVE" if boundary == "restart" else "STOPPED")
    run(scenario())
