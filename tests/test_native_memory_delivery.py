"""A generated native answer still requires its original memory proof at send-check."""

import json

import httpx
import pytest

from companion.store import after
from companion_delivery_protocol import digest
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, call, worker_for
from tests.support.core_http import real_core_http
from tests.support.owner_memory import environment

FACT = "青禾项目已经选择蓝色配色。"


@pytest.mark.parametrize("drift,boundary", [
    ("none", "before-lease"),
    *[(drift, boundary) for drift in ("ttl", "fact", "proof")
      for boundary in ("before-lease", "final-check")],
])
def test_native_reply_rechecks_memory_after_generation(core, tmp_path, drift, boundary):
    async def scenario():
        async with environment(core, tmp_path, history=False) as (admit, _, authority):
            model = Provider(answer(calls=[call("recall-color", "memory_recall", {"query": "*"})]),
                             answer("青禾项目的当前配色是蓝色。"))
            async with worker_for(core, model) as worker:
                await admit(1, "/记住这个 " + FACT)
                assert await worker.run_once("seed-native-memory")
                source, _ = await admit(2, "项目是什么配色？")
                core.store.execute("UPDATE memories SET valid_to=?", (after(core.clock(), 2),))
                assert await worker.run_once("generate-source-bound-answer")
            assert len(model.requests) == 2
            assert any(message["role"] == "tool" and FACT in message["content"]
                       for message in model.requests[-1]["messages"])
            usage = core.store.rows("SELECT * FROM model_usage")
            assert len(usage) == 2 and all(row["state"] == "SETTLED" for row in usage)

            def change():
                if drift == "ttl":
                    core.clock.advance(3)
                elif drift == "fact":
                    core.store.execute("UPDATE memories SET content='事实已改变，旧答案失效'")
                elif drift == "proof":
                    rows = core.store.rows("SELECT job_id,snapshots_json FROM agent_conversations")
                    for row in rows:
                        snapshots = json.loads(row["snapshots_json"])
                        core.store.execute("UPDATE agent_conversations SET snapshots_json=? "
                            "WHERE job_id=?", (json.dumps([item for item in snapshots
                                if item["kind"] != "memory"]), row["job_id"]))

            async with real_core_http(core) as port, httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False,
                headers={"Authorization": "Bearer "
                         + core.settings.bridge_token.get_secret_value()},
            ) as client:
                if boundary == "before-lease":
                    change()
                leased = await client.post("/internal/v1/deliveries/lease",
                    json={"worker_id": "native-memory-delivery", "authority": authority})
                assert leased.status_code == 200, leased.text
                items = [item for item in leased.json()["items"]
                         if item["origin_event_id"] == source.event_id]
                if not items:
                    assert drift != "none"
                else:
                    item = items[0]
                    begun = await client.post(
                        f"/internal/v1/deliveries/{item['message_id']}/begin-send",
                        json={"lease_token": item["lease_token"]})
                    if begun.status_code != 200:
                        assert drift != "none" and begun.status_code in {403, 409}
                    else:
                        if boundary == "final-check":
                            change()
                        checked = await client.post(
                            "/internal/v1/owner-authority/send-check", json={
                            "authority": authority, "message_id": item["message_id"],
                            "event_id": source.event_id, "lease_token": item["lease_token"],
                            "content_sha256": digest(item["content"]),
                            "reply_revision": item["reply_revision"]})
                        assert core.store.rows("SELECT * FROM model_usage") == usage
                        assert (checked.status_code == 200) is (drift == "none"), checked.text
                        if drift != "none":
                            assert checked.status_code in {403, 409}
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert not core.store.rows("SELECT 1 FROM outbox WHERE state='SENT'")
    run(scenario())
