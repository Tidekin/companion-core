"""Real HTTP, independent model and reconciliation workers, synthetic provider I/O."""

import asyncio
import json
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from companion import paid_budget
from companion_onebot_bridge import journal, owner_native_resolution
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.network import CheckedTransport, PrivateDestination
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerSendGuard
from companion_onebot_bridge.spool import Spool, encode
from tests.support.onebot_events import raw_event
from tests.test_bridge_owner_runtime import config as config
from tests.test_evaluation_process import process_config
from tests.test_reading_followup import window


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


def test_native_unknown_requires_dedicated_stop_proof_and_original_owner_receipt(
    core, tmp_path, monkeypatch,
):
    core.settings.control_token = SecretStr("synthetic-owner-control-token")
    core.settings.console.enabled = True
    core.settings.console.public_origin = "https://console.example.test"

    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, base, plan):
            config = replace(base, owner_authority=plan.authority,
                capture_signing_key=plan.key.private_bytes_raw().hex(),
                data_dir=str(tmp_path / "native-bridge"), core_url=str(client.base_url).rstrip("/"))
            with Spool(config) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                raw = encode(raw_event(1, time=int(core.clock.now.timestamp()),
                                       text="测试未知发送"))
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
                worked = await child("tests.owner_runtime_process", "--config", str(worker_config),
                                     "--now", core.clock())
                assert worked == {"worked": True, "calls": 1}
                response = await client.post("/internal/v1/deliveries/lease", json={
                    "worker_id": "bridge", "authority": plan.authority, "limit": 1})
                assert response.status_code == 200, response.text
                item = response.json()["items"][0]
                guard = OwnerSendGuard(runtime, client)
                guard.reserve(item)
                begun = await client.post(
                    f"/internal/v1/deliveries/{item['message_id']}/begin-send",
                    json={"lease_token": item["lease_token"]})
                assert begun.status_code == 200, begun.text
                spool.start_delivery(item)
                calls = []

                def lost_response(request):
                    calls.append(request.url.path)
                    raise httpx.ReadTimeout("synthetic lost response")

                transport = CheckedTransport(httpx.MockTransport(lost_response),
                                             guard.before_dispatch)
                async with httpx.AsyncClient(transport=transport) as protocol:
                    request = protocol.build_request("POST", "http://127.0.0.1:3000/send_private_msg",
                        headers={"Host": "snowluma:3000",
                                 "Authorization": "Bearer " + config.onebot_token},
                        json={"user_id": int(config.owner_qq_id), "message": [
                            {"type": "text", "data": {"text": item["content"]["text"]}}],
                            "auto_escape": True})
                    request.extensions.update(sni_hostname="snowluma",
                        companion_private_destination=PrivateDestination(
                            "snowluma:3000", "http", "snowluma", 3000, "127.0.0.1"))
                    try:
                        await protocol.send(request)
                    except httpx.ReadTimeout:
                        pass
                assert calls == ["/send_private_msg"]
                guard.complete(item, None)
                assert await spool.flush_result(client)
                stopped = await client.post("/internal/v1/owner-authority/stop", json={
                    "authority": plan.authority, "operation_id": "stop-native-bridge-unknown"},
                    headers={"Authorization": "Bearer synthetic-owner-control-token"})
                assert stopped.status_code == 200, stopped.text
                before_usage = core.store.rows("SELECT * FROM model_usage")
            bridge_config = tmp_path / "bridge.json"
            bridge_config.write_text(json.dumps(asdict(config)), "utf-8")
            core.clock.advance(60)
            args = ("--config", str(bridge_config), "--now", core.clock(),
                    "--message-id", item["message_id"])
            prepared = await child("tests.owner_reconcile_process", *args, "--operation", "prepare")
            assert prepared["state"] == "READY"
            waiting = await child("tests.owner_reconcile_process", *args,
                                  "--operation", "reconcile")
            assert waiting["state"] == "AWAITING_OWNER"
            login = await client.post("/v1/console/session",
                json={"password": "synthetic-owner-control-token"},
                headers={"Origin": core.settings.console.public_origin})
            assert login.status_code == 200, login.text
            resolved = await client.post(f"/v1/console/deliveries/{item['message_id']}/reconcile",
                json={"outcome": "CANCELLED"}, headers={
                    "Origin": core.settings.console.public_origin,
                    "X-CSRF-Token": login.json()["csrf_token"],
                    "Cookie": "companion_owner_session=" + login.cookies["companion_owner_session"],
                    "Idempotency-Key": "owner-native-bridge-resolution"})
            assert resolved.status_code == 200, resolved.text
            accepted = await child("tests.owner_reconcile_process", *args,
                                   "--operation", "reconcile")
            assert accepted["state"] == "OWNER_RESOLVED"
            assert accepted["outcome"] == "CANCELLED"
            assert core.store.rows("SELECT * FROM model_usage") == before_usage
            paid_budget.journal(core.store, core.settings.paid_model, allow_settlement=True)
            with Spool(config, reconciliation_only=True) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                records = runtime.verify(active=False)
                assert records[-1]["kind"] == "OWNER_RECONCILED"
                assert not await spool.flush_result(client)
                assert "测试未知发送" not in runtime.path.read_text("utf-8")
                assert journal.read(runtime.path)[1] == runtime.row()["journal_head"]
    asyncio.run(scenario())


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


@pytest.mark.parametrize("mutation", [None, "lease", "missing_ack", "native_ack", "queue",
                                     "reserved", "unknown", "authority"])
def test_saved_success_ack_requires_original_attempt_and_durable_result(config, mutation):
    now = datetime.now(UTC)
    item = {"authority": config.owner_authority, "message_id": "msg_" + "a" * 32,
        "lease_token": "lease_" + "b" * 32, "lease_until": (now + timedelta(minutes=2)).isoformat(),
        "origin_event_id": "evt_" + "c" * 64, "action_type": "qq.private.proactive",
        "content": {"text": "synthetic saved acknowledgement"}}
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        OwnerSendGuard(runtime, None).reserve(item)
        spool.start_delivery(item)
        if mutation != "reserved":
            with spool.db:
                runtime.append("IO_STARTED", {"message_id": item["message_id"],
                                               "provider_ack_hash": None})
                spool.db.execute("UPDATE window_attempts SET state='IO_STARTED'")
        spool.finish_delivery(item["message_id"], "synthetic-provider-id")
        if mutation in {"lease", "missing_ack", "native_ack"}:
            body = json.loads(spool.db.execute("SELECT body FROM results").fetchone()[0])
            if mutation == "lease":
                body["lease_token"] = "lease_" + "d" * 32
            elif mutation == "missing_ack":
                body["provider_message_id"] = None
            else:
                body["native_ack"] = {"action": "friend_poke"}
            with spool.db:
                spool.db.execute("UPDATE results SET body=?", (encode(body),))
        elif mutation == "queue":
            with spool.db:
                spool.db.execute("UPDATE results SET state='rejected'")
        elif mutation == "unknown":
            with spool.db:
                runtime.append("UNKNOWN", {"message_id": item["message_id"],
                                            "provider_ack_hash": None})
                spool.db.execute("UPDATE window_attempts SET state='UNKNOWN'")
        runtime.stop()
        records = runtime.verify(active=False)
        if mutation == "authority":
            runtime.authority = runtime.authority | {"revision": 3}
        if mutation:
            with pytest.raises(ValueError, match="DELIVERY_SAVED_ACK_BINDING_REQUIRED"):
                owner_native_resolution._saved_ack(runtime, records, item["message_id"])
        else:
            saved = owner_native_resolution._saved_ack(runtime, records, item["message_id"])
            assert saved["attempt_state"] == "IO_STARTED" and saved["state"] == "pending"
            assert saved["provider_ack_sha256"] == journal.digest("synthetic-provider-id")


def test_active_bridge_cannot_prove_quiescence(config):
    with Spool(config) as spool:
        runtime = OwnerRuntime(spool)
        runtime.stop()
        with pytest.raises(ValueError, match="DELIVERY_QUIESCENT_PROCESS_REQUIRED"):
            asyncio.run(owner_native_resolution.prepare(runtime, None, "msg_" + "a" * 32))


@pytest.mark.parametrize("mutation", ["proof", "missing", "stop-successor"])
def test_native_bridge_resolution_rejects_changed_custody(config, mutation):
    item = reserve_uncertain(config)
    with Spool(config, reconciliation_only=True) as spool:
        runtime = OwnerRuntime(spool, clock=lambda: datetime.now(UTC) + timedelta(seconds=1))

        def core(request):
            if request.url.path.endswith("stop-receipt"):
                successor = runtime.authority | {
                    "revision": 2 if mutation != "stop-successor" else 3}
                return httpx.Response(200, json={"state": "STOPPED", "authority": runtime.authority,
                    "stopped_authority": successor, "core_stop_sha256": "e" * 64,
                    "budget_journal_id": "budget_" + "f" * 32, "quiescence_proven": False})
            proof = json.loads(request.content)
            return httpx.Response(200, json={"state": "READY", "message_id": item["message_id"],
                "quiescence_sha256": journal.digest(proof)})

        async def prepare():
            async with httpx.AsyncClient(base_url="http://127.0.0.1:8765",
                                         transport=httpx.MockTransport(core)) as client:
                return await owner_native_resolution.prepare(runtime, client, item["message_id"])

        if mutation == "stop-successor":
            with pytest.raises(ValueError, match="OWNER_CORE_STOP_RECEIPT_INVALID"):
                asyncio.run(prepare())
            assert not owner_native_resolution.rows(runtime)
            return
        assert asyncio.run(prepare())["state"] == "READY"
        if mutation == "missing":
            spool.db.execute("DELETE FROM window_delivery_resolutions")
        else:
            spool.db.execute("UPDATE window_delivery_resolutions SET proof_json='{}'")
        with pytest.raises(ValueError, match="DELIVERY_RESOLUTION_JOURNAL_REQUIRED"):
            runtime.verify(active=False)
