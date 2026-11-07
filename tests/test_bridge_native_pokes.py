"""Independent Core worker and the real Bridge sender with synthetic OneBot transport."""

import asyncio
import json
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from companion_onebot_bridge import owner_pokes
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from companion_onebot_bridge.spool import Spool, encode
from tests.poke_authority_fixture import environment
from tests.support.bridge_owner_resolution import child
from tests.support.core_http import real_core_http
from tests.support.onebot import provider
from tests.support.onebot_events import raw_event
from tests.test_evaluation_process import process_config


@pytest.mark.parametrize("outcome", ["sent", "unknown", "new-owner-input"])
def test_native_poke_capture_worker_and_checked_friend_poke(core, tmp_path, outcome):
    core.clock.now = datetime.now(UTC).replace(microsecond=0)
    env = environment(core, tmp_path)
    core.settings.control_token = SecretStr("synthetic-owner-control-token")
    core.settings.console.enabled = True
    core.settings.console.public_origin = "https://console.example.test"
    config = replace(env.config, owner_authority=env.approval.authority.as_dict(),
        capture_signing_key=env.key.private_bytes_raw().hex(),
        data_dir=str(tmp_path / "poke-bridge"), onebot_http_url="http://127.0.0.1:3000",
        onebot_ws_url="ws://127.0.0.1:3001")
    worker_config, output = tmp_path / "worker.json", tmp_path / "worker-result.json"
    process_config(core, worker_config)
    sends = []

    async def run():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + config.bridge_token},
        ) as client:
            with Spool(config) as spool:
                runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                frame = encode({"post_type": "notice", "notice_type": "notify", "sub_type": "poke",
                    "time": int(core.clock.now.timestamp()), "self_id": 10002, "user_id": 10001,
                    "sender_id": 10001, "target_id": 10002, "action": "拍了拍", "suffix": ""})
                assert await owner_pokes.put(runtime, client, frame, core.clock.now,
                                             ("127.0.0.1", 1))
                assert await runtime.flush_one(client)
                assert await owner_pokes.put(runtime, client, frame, core.clock.now,
                                             ("127.0.0.1", 1))
                assert not await runtime.flush_one(client)
                core.clock.advance(3)
                process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
                    "tests.poke_native_process", str(worker_config), core.clock(),
                    str(output), "poke",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    async with asyncio.timeout(45):
                        _, error = await process.communicate()
                        assert process.returncode == 0, error.decode()
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                assert len(json.loads(output.read_text("utf-8"))["calls"]) == 1
                usage = core.store.rows("SELECT * FROM model_usage")

                async def late_owner(request):
                    if outcome == "new-owner-input" and request.url.path.endswith("send-check"):
                        raw = encode(raw_event(2, time=int(core.clock.now.timestamp()),
                                               text="新输入"))
                        event = normalize(raw, config, now=core.clock.now.timestamp())
                        runtime.persist_capture(event, raw, received_at=core.clock.now,
                                                peer=("127.0.0.1", 1))

                client.event_hooks["request"].append(late_owner)

                def onebot(request):
                    if request.url.path == "/friend_poke":
                        sends.append(json.loads(request.content))
                        if outcome == "unknown":
                            raise httpx.ReadTimeout("synthetic lost poke ack")
                        return httpx.Response(200, json={
                            "status": "ok", "retcode": 0, "data": None})
                    return provider(request, [])

                async with private_http_client(config.onebot_http_url, config.onebot_token,
                    transport=httpx.MockTransport(onebot)) as protocol:
                    guard = OwnerSendGuard(runtime, client)
                    assert await deliver_tick(client, OwnerOneBot(guard, protocol),
                                              spool, guard=guard)
                expected = ("SENT" if outcome == "sent" else "CANCELLED"
                            if outcome == "new-owner-input" else "DELIVERY_UNCERTAIN")
                stored = core.store.one("SELECT * FROM outbox")
                assert stored["state"] == expected
                assert stored["attempt_count"] == 1
                assert len(sends) == (0 if outcome == "new-owner-input" else 1)
                if sends:
                    assert sends == [{"user_id": 10001, "target_id": 10001}]
                if outcome == "sent":
                    assert json.loads(stored["native_ack_json"])["action"] == "friend_poke"
                    assert stored["provider_message_id"] is None
                assert core.store.rows("SELECT * FROM model_usage") == usage
                saved = spool.db.execute("SELECT capture_json FROM window_inputs "
                    "WHERE json_extract(capture_json,'$.kind')=?",
                    ("authenticated_onebot_poke",)).fetchone()
                original = json.loads(saved[0])
                state = await client.post("/internal/v1/owner-authority/inputs/state",
                                          json=original)
                assert state.status_code == 200, state.text
                assert state.json()["terminal"] == (outcome != "unknown")
                assert state.json()["input_sha256"] == core.store.one(
                    "SELECT fingerprint FROM events WHERE event_type='notice.poke'")["fingerprint"]
                runtime.verify(active=False)
                if outcome == "unknown":
                    stopped = await client.post("/internal/v1/owner-authority/stop", json={
                        "authority": runtime.authority, "operation_id": "stop-native-poke"},
                        headers={"Authorization": "Bearer synthetic-owner-control-token"})
                    assert stopped.status_code == 200, stopped.text
            if outcome == "unknown":
                bridge_config = tmp_path / "bridge.json"
                bridge_config.write_text(json.dumps(asdict(replace(config,
                    core_url=str(client.base_url).rstrip("/")))), "utf-8")
                core.clock.advance(60)
                args = ("--config", str(bridge_config), "--now", core.clock(),
                        "--message-id", stored["message_id"])
                prepared = await child("tests.owner_reconcile_process", *args,
                                       "--operation", "prepare")
                assert prepared["state"] == "READY"
                waiting = await child("tests.owner_reconcile_process", *args,
                                      "--operation", "reconcile")
                assert waiting["state"] == "AWAITING_OWNER"
                login = await client.post("/v1/console/session",
                    json={"password": "synthetic-owner-control-token"},
                    headers={"Origin": core.settings.console.public_origin})
                assert login.status_code == 200, login.text
                resolved = await client.post(
                    f"/v1/console/deliveries/{stored['message_id']}/reconcile",
                    json={"outcome": "CANCELLED"}, headers={
                        "Origin": core.settings.console.public_origin,
                        "X-CSRF-Token": login.json()["csrf_token"],
                        "Cookie": "companion_owner_session=" + login.cookies[
                            "companion_owner_session"],
                        "Idempotency-Key": "owner-native-poke-resolution"})
                assert resolved.status_code == 200, resolved.text
                accepted = await child("tests.owner_reconcile_process", *args,
                                       "--operation", "reconcile")
                assert accepted["state"] == "OWNER_RESOLVED"
                assert accepted["outcome"] == "CANCELLED"
                assert core.store.rows("SELECT * FROM model_usage") == usage
                with Spool(config, reconciliation_only=True) as spool:
                    runtime = OwnerRuntime(spool, clock=lambda: core.clock.now)
                    proof = next(row["data"] for row in runtime.verify(active=False)
                                 if row["kind"] == "DELIVERY_QUIESCED")
                    assert proof["payload_kind"] == "content_sha256"
                    assert proof["reply_revision"] is None
                    assert proof["authority"] == env.approval.authority.as_dict()
                    assert not await spool.flush_result(client)
    asyncio.run(run())
