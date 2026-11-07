"""Real HTTP, separate Core worker and stopped Bridge process; no platform replay."""

import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from companion import paid_budget
from companion.transport import recover_deliveries
from companion_onebot_bridge import delivery_receipts, owner_handoff, owner_native_resolution
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from companion_onebot_bridge.spool import Spool, encode
from tests.support.bridge_owner_resolution import child
from tests.support.evaluation_process import process_config
from tests.support.onebot import provider
from tests.support.onebot_events import raw_event
from tests.support.reading_followup import window


@pytest.mark.parametrize("lost_ack", [False, True])
@pytest.mark.parametrize("superseded", [False, True])
def test_reserved_before_begin_send_closes_and_handoffs_without_owner_guess(
    core, tmp_path, monkeypatch, lost_ack, superseded,
):
    core.settings.control_token = SecretStr("synthetic-owner-control-token")
    owner = {"Authorization": "Bearer synthetic-owner-control-token"}

    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, base, plan):
            config = replace(base, owner_authority=plan.authority,
                capture_signing_key=plan.key.private_bytes_raw().hex(),
                data_dir=str(tmp_path / "native-bridge"), core_url=str(client.base_url).rstrip("/"),
                onebot_http_url="http://127.0.0.1:3000", onebot_ws_url="ws://127.0.0.1:3001")
            with Spool(config) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                raw = encode(raw_event(1, time=int(core.clock.now.timestamp()),
                                       text="未开始发送的合成输入"))
                event = normalize(raw, config, now=core.clock.now.timestamp())
                runtime.persist_capture(event, raw, received_at=core.clock.now,
                                        peer=("127.0.0.1", 1))
                assert await runtime.flush_one(client)
                worker_config = tmp_path / "worker.json"
                process_config(core, worker_config)
                exported = json.loads(worker_config.read_text("utf-8"))
                for name, adapter in core.settings.observation_adapters.items():
                    exported["observation_adapters"][name]["token"] = (
                        adapter.token.get_secret_value())
                worker_config.write_text(json.dumps(exported), "utf-8")
                assert await child("tests.owner_runtime_process", "--config", str(worker_config),
                                   "--now", core.clock()) == {"worked": True, "calls": 1}
                leased = await client.post("/internal/v1/deliveries/lease", json={
                    "worker_id": "bridge", "authority": plan.authority, "limit": 1})
                assert leased.status_code == 200, leased.text
                item = leased.json()["items"][0]
                OwnerSendGuard(runtime, client).reserve(item)
                assert not spool.db.execute("SELECT 1 FROM results").fetchone()
                if superseded:
                    newer = encode(raw_event(91, time=int(core.clock.now.timestamp()),
                                             text="先等一下，我补充一句"))
                    received = normalize(newer, config, now=core.clock.now.timestamp())
                    runtime.persist_capture(received, newer, received_at=core.clock.now,
                                            peer=("127.0.0.1", 1))
                    assert await runtime.flush_one(client)
            # The stopped process lost its in-memory item. Lease expiry clears
            # the token; supersession retains the original token as evidence.
            core.clock.advance(core.settings.lease_seconds + 1)
            recover_deliveries(core.store, core.clock())
            assert core.store.one((
                'SELECT state,attempt_count,lease_token FROM outbox WHERE '
                'message_id=%s'
            ), (item["message_id"],)) == {
                    "state": "CANCELLED" if superseded else "PENDING", "attempt_count": 0,
                    "lease_token": item["lease_token"] if superseded else None}
            stopped = await client.post("/internal/v1/owner-authority/stop", headers=owner,
                json={"authority": plan.authority, "operation_id": "stop-reserved-process"})
            assert stopped.status_code == 200, stopped.text
            usages = core.store.rows("SELECT * FROM model_usage")
            budget_path = Path(core.settings.paid_model.journal_path)
            budget = await asyncio.to_thread(budget_path.read_bytes)
            if lost_ack:
                original_post = owner_native_resolution.bounded_post

                async def lose_response(client, path, body):
                    result = await original_post(client, path, body)
                    if path == "/internal/v1/deliveries/reconciliation/prepare":
                        raise httpx.ReadTimeout("synthetic Core response lost")
                    return result

                with Spool(config, reconciliation_only=True) as spool:
                    runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                    with monkeypatch.context() as patch:
                        patch.setattr(owner_native_resolution, "bounded_post", lose_response)
                        with pytest.raises(httpx.ReadTimeout):
                            await owner_native_resolution.prepare(runtime, client,
                                                                  item["message_id"])
            bridge_config = tmp_path / "bridge.json"
            bridge_config.write_text(json.dumps(asdict(config)), "utf-8")
            core.clock.advance(1)
            args = ("--config", str(bridge_config), "--now", core.clock(),
                    "--message-id", item["message_id"])
            prepared = await child("tests.owner_reconcile_process", *args, "--operation", "prepare")
            assert prepared["state"] == "NOT_SENT" and prepared["paused"]
            assert await child("tests.owner_reconcile_process", *args,
                               "--operation", "prepare") == prepared
            assert await child("tests.owner_reconcile_process", *args,
                               "--operation", "reconcile") == prepared
            assert core.store.rows("SELECT * FROM model_usage") == usages
            assert await asyncio.to_thread(budget_path.read_bytes) == budget
            assert not core.store.one("SELECT 1 FROM console_operations")
            with Spool(config, reconciliation_only=True) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                records = runtime.verify(active=False)
                assert records[-1]["kind"] == "NOT_SENT"
                body, state = spool.db.execute("SELECT body,state FROM results").fetchone()
                assert state == "accepted" and json.loads(body)["result"] == "NOT_SENT"
                assert "lease_token" not in json.loads(body)
                assert not await spool.flush_result(client)
                assert (await owner_handoff.prepare(runtime, client))["state"] == "HANDOFF_PREPARED"
                value = {"expected_revision": 2, "policy": plan.policy.model_dump(mode="json")}
                preview = await client.post("/internal/v1/owner-authority/preview",
                                            json=value, headers=owner)
                assert preview.status_code == 200, preview.text
                activated = await client.post("/internal/v1/owner-authority/activate",
                    headers=owner,
                    json=value | {"operation_id": "reopen-after-not-sent",
                                  "preview_sha256": preview.json()["preview_sha256"]})
                assert activated.status_code == 200, activated.text
                successor = activated.json()["authority"]
                result = await owner_handoff.handoff(runtime, client, successor)
                assert result["state"] == "ACTIVE"
            with Spool(replace(config, owner_authority=successor)) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                runtime.verify()
                assert not await runtime.flush_one(client)
                assert not await spool.flush_result(client)
                assert "未开始发送的合成输入" not in runtime.path.read_text("utf-8")
                assert not core.store.one("SELECT 1 FROM outbox WHERE state='SENT'")
                assert not core.store.one("SELECT 1 FROM audit_log WHERE action='delivery.result'")
                # A completed recovery remains in the spool while the successor
                # receives and sends new work. Receipt lookup must not invoke the
                # startup recovery fence on this process's in-flight send.
                raw = encode(raw_event(2, time=int(core.clock.now.timestamp()),
                                       text="交接后的新输入"))
                event = normalize(raw, spool.config, now=core.clock.now.timestamp())
                runtime.persist_capture(event, raw, received_at=core.clock.now,
                                        peer=("127.0.0.1", 1))
                assert await runtime.flush_one(client)
                assert await child("tests.owner_runtime_process", "--config", str(worker_config),
                                   "--now", core.clock()) == {"worked": True, "calls": 1}
                sends = []
                transport = httpx.MockTransport(lambda request: provider(request, sends))
                async with private_http_client(config.onebot_http_url, config.onebot_token,
                                               transport=transport) as protocol:
                    guard = OwnerSendGuard(runtime, client)
                    assert await deliver_tick(client, OwnerOneBot(guard, protocol),
                                              spool, guard=guard)
                runtime.verify()
                assert len(sends) == 1
                assert spool.db.execute("SELECT state FROM window_attempts ORDER BY fence DESC "
                                        "LIMIT 1").fetchone() == ("ACK",)
                assert core.store.one((
                    'SELECT state,attempt_count FROM outbox WHERE '
                    'origin_event_id=%s'
                ), (event["event_id"],)) == {
                        "state": "SENT", "attempt_count": 1}
                raw = encode(raw_event(3, time=int(core.clock.now.timestamp()),
                                       text="保留已发送回执"))
                event = normalize(raw, spool.config, now=core.clock.now.timestamp())
                runtime.persist_capture(event, raw, received_at=core.clock.now,
                                        peer=("127.0.0.1", 1))
                assert await runtime.flush_one(client)
                assert await child("tests.owner_runtime_process", "--config", str(worker_config),
                                   "--now", core.clock()) == {"worked": True, "calls": 1}
                original_result = delivery_receipts.bounded_post

                async def lose_result(client, path, body, **kwargs):
                    if path.endswith("/result"):
                        if lost_ack:
                            await original_result(client, path, body, **kwargs)
                        raise httpx.ReadTimeout("synthetic result response lost")
                    return await original_result(client, path, body, **kwargs)

                async with private_http_client(config.onebot_http_url, config.onebot_token,
                                               transport=transport) as protocol:
                    guard = OwnerSendGuard(runtime, client)
                    with monkeypatch.context() as patch:
                        patch.setattr(delivery_receipts, "bounded_post", lose_result)
                        with pytest.raises(httpx.ReadTimeout):
                            await deliver_tick(client, OwnerOneBot(guard, protocol),
                                               spool, guard=guard)
                assert len(sends) == 2
                ack_message = spool.db.execute("SELECT message_id FROM window_attempts "
                                               "ORDER BY fence DESC LIMIT 1").fetchone()[0]
                saved, queue = spool.db.execute("SELECT body,state FROM results WHERE message_id=?",
                                                (ack_message,)).fetchone()
                assert queue == "pending" and json.loads(saved)["result"] == "SENT"
            core.clock.advance(core.settings.lease_seconds + 1)
            recover_deliveries(core.store, core.clock())
            stopped = await client.post("/internal/v1/owner-authority/stop", headers=owner,
                json={"authority": successor, "operation_id": "stop-retained-success-ack"})
            assert stopped.status_code == 200, stopped.text
            fees = core.store.rows("SELECT * FROM model_usage")
            if lost_ack:
                post = owner_native_resolution.bounded_post

                async def lose_recovery_result(client, path, body):
                    result = await post(client, path, body)
                    if path.endswith("/result"):
                        raise httpx.ReadTimeout("synthetic recovery response lost")
                    return result

                with Spool(replace(config, owner_authority=successor),
                           reconciliation_only=True) as spool:
                    runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                    with monkeypatch.context() as patch:
                        patch.setattr(owner_native_resolution, "bounded_post", lose_recovery_result)
                        with pytest.raises(httpx.ReadTimeout):
                            await owner_native_resolution.prepare(runtime, client, ack_message)
            bridge_config.write_text(
                json.dumps(asdict(replace(config, owner_authority=successor))), "utf-8")
            args = ("--config", str(bridge_config), "--now", core.clock(),
                    "--message-id", ack_message, "--operation", "prepare")
            receipt = await child("tests.owner_reconcile_process", *args)
            assert receipt["state"] == "ACK" and receipt["paused"]
            assert receipt["core_result_confirmed"]
            assert await child("tests.owner_reconcile_process", *args) == receipt
            assert len(sends) == 2
            assert core.store.rows("SELECT * FROM model_usage") == fees
            assert core.store.one('SELECT state,attempt_count FROM outbox WHERE message_id=%s',
                                   (ack_message,)) == {"state": "SENT", "attempt_count": 1}
            with Spool(replace(config, owner_authority=successor),
                       reconciliation_only=True) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                assert spool.db.execute("SELECT body,state FROM results WHERE message_id=?",
                                       (ack_message,)).fetchone() == (saved, "accepted")
                assert (await owner_handoff.prepare(runtime, client))["state"] == "HANDOFF_PREPARED"
            paid_budget.journal(core.store, core.settings.paid_model, allow_settlement=True)
    asyncio.run(scenario())
