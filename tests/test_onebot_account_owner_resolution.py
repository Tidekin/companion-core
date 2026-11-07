"""Account UNKNOWN responsibility closes without synthesizing a private owner turn."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

import companion_delivery_protocol as wire
from companion import delivery_reconciliation, owner_authority
from companion_onebot_bridge import owner_native_resolution as owner_resolution
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.native_continuous_support import host
from tests.native_owner_recovery_support import CONTROL_TOKEN, stop_host
from tests.support.onebot_account_actions import catalog, fixture, model_response, plan_action
from tests.support.onebot_owner_resolution import (
    assert_custody_counts,
    original_rows,
    reopen,
    successor,
)
from tests.support.onebot_pokes import silent


@pytest.mark.parametrize("kind,path", [
    ("qzone_publish", "/send_qzone_msg"), ("nickname", "/set_qq_profile"),
    ("signature", "/set_self_longnick"), ("avatar", "/set_qq_avatar"),
])
@pytest.mark.parametrize("outcome", ["SENT", "CANCELLED"])
def test_account_owner_resolution_preserves_native_unknown_and_handoffs_without_fake_input(
    core, tmp_path, monkeypatch, kind, path, outcome,
):
    core.clock.now = datetime.now(UTC).replace(microsecond=0)
    ref, conversation, options, proposal = fixture(core, kind)
    sends = []

    def native(request):
        if request.url.path == path:
            sends.append(request)
            raise httpx.ReadTimeout("synthetic account acknowledgement lost")

    async def scenario():
        directory = tmp_path / "reviewed"
        bridge_options = {"account_asset_directory": str(directory),
            "account_asset_policy_sha256": "e" * 64} if kind == "avatar" else {}
        async with host(core, tmp_path, silent, protocol_reply=native,
            model_reply=lambda _: model_response(proposal), bridge_options=bridge_options,
            core_options={**options["core_options"], "control_token": SecretStr(CONTROL_TOKEN)},
            purposes=("chat", "social", "account_actions")) as (active, _, private_sends, calls):
            policy = owner_authority.current(core.store, core.settings, core.clock).policy
            client = active.client
            review, _, _ = catalog(core, directory) if kind == "avatar" else (None, None, None)
            frozen = await plan_action(core, ref, conversation, proposal, review=review)
            guard = OwnerSendGuard(active.runtime, client)
            assert await deliver_tick(client, OwnerOneBot(guard, active.protocol), active.spool,
                                      guard=guard, delivery_lane="qq_account")
            await stop_host(active)
            attempt = original_rows(active.runtime, "window_attempts")[0]
            result = original_rows(active.runtime, "results")[0]
            usage = core.store.rows("SELECT * FROM model_usage")
            account_receipt = core.store.one(
                "SELECT receipt_json FROM social_autonomy_reservations")["receipt_json"]
            origin = core.store.one("SELECT origin_event_id FROM outbox")["origin_event_id"]
            assert attempt["state"] == "UNKNOWN" and attempt["event_id"] == frozen.plan_id
            assert active.captures() == 0 and len(sends) == 1 and not private_sends
            env = SimpleNamespace(active=active, cfg=active.config, policy=policy, client=client,
                                  core=core, monkeypatch=monkeypatch)
            old = reopen(env)
            with old.spool:
                assert (await owner_resolution.prepare(old, client, frozen.message_id))[
                    "state"] == "READY"
                proof = json.loads(owner_resolution.rows(old)[0]["proof_json"])
                assert proof["account_plan_id"] == frozen.plan_id
                assert proof["payload_kind"] == "account_payload_sha256"
                assert "event_id" not in proof and "reply_revision" not in proof
                chosen = delivery_reconciliation.resolve(core.store, core.settings,
                    frozen.message_id, outcome, core.clock(), operation_id="account-owner-decision",
                    operation_sha256=wire.digest([frozen.message_id, outcome]))
                assert chosen["receipt"]["input_closure"] is None
                assert chosen["receipt"]["delivery"]["event_id"] == origin
                if kind != "qzone_publish":
                    assert origin is None
                assert (await owner_resolution.reconcile(old, client, frozen.message_id))[
                    "outcome"] == outcome
                assert original_rows(old, "window_attempts")[0] == attempt
                assert original_rows(old, "results")[0] == result
                assert core.store.one("SELECT receipt_json FROM social_autonomy_reservations")[
                    "receipt_json"] == account_receipt
                assert_custody_counts(old, inputs=0)
                config = await successor(env, old)
                old.spool.close()
                with Spool(config) as spool:
                    new = OwnerRuntime(spool, clock=lambda: core.clock.now)
                    new.verify()
                    assert_custody_counts(new, inputs=0)
                    assert original_rows(new, "window_attempts")[0] == attempt
                    assert original_rows(new, "results")[0] == result
                    assert len(sends) == 1 and len(calls) == 1
                    assert core.store.rows("SELECT * FROM model_usage") == usage


    run(scenario())
