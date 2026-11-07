"""Generated Reader answers retain the original observation through final send-check."""

import json

import pytest

from companion.store import encode
from companion_delivery_protocol import digest
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, worker_for
from tests.support.exploration import URL
from tests.support.reading_followup import admit, finish, lease, select_read, window


@pytest.mark.parametrize("drift,boundary", [
    ("none", "before_lease"),
    *[(drift, boundary) for drift in ("body", "evidence", "binding")
      for boundary in ("before_lease", "final_check")],
])
def test_native_reader_reply_revalidates_observation_after_generation(
    core, tmp_path, monkeypatch, drift, boundary,
):
    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, config, grant):
            event, accepted = await admit(client, core, config, grant, 1, card=URL)
            selection = await select_read(core)
            item = await lease(client)
            completed, _ = await finish(client, core, item)
            observed = completed["event_ids"][0]
            model = Provider(answer("根据实际读取正文整理的回答。"))
            async with worker_for(core, model) as worker:
                assert await worker.run_once("generate-reader-answer", job_id=accepted["job_id"])
            assert len(selection.requests) == len(model.requests) == 1
            returned = [json.loads(message["content"]) for message in model.requests[0]["messages"]
                        if message["role"] == "tool"]
            assert any(row["event_id"] == observed
                       for value in returned for row in value["items"])
            usage = core.store.rows("SELECT * FROM model_usage")
            assert len(usage) == 2 and all(row["state"] == "SETTLED" for row in usage)

            def change():
                if drift == "body":
                    row = core.store.one('SELECT body_json FROM events WHERE event_id=%s',
                                         (observed,))
                    value = json.loads(row["body_json"]) | {"text": "正文已经变化"}
                    core.store.execute('UPDATE events SET body_json=%s WHERE event_id=%s',
                                       (encode(value), observed))
                elif drift == "evidence":
                    core.store.execute((
                        "UPDATE observations SET evidence_json='{}' WHERE "
                        'event_id=%s'
                    ), (observed,))
                elif drift == "binding":
                    core.store.execute((
                        'DELETE FROM exploration_evidence WHERE request_id=%s'
                        ' AND event_id=%s'
                    ), (item["request_id"], observed))

            if boundary == "before_lease":
                change()
            leased = await client.post("/internal/v1/deliveries/lease", json={
                "worker_id": "reader-proof-delivery", "authority": grant.authority})
            assert leased.status_code == 200, leased.text
            items = leased.json()["items"]
            approved = False
            if items:
                delivery = items[0]
                begun = await client.post(
                    f"/internal/v1/deliveries/{delivery['message_id']}/begin-send",
                    json={"lease_token": delivery["lease_token"]})
                assert begun.status_code == 200, begun.text
                if boundary == "final_check":
                    change()
                checked = await client.post("/internal/v1/owner-authority/send-check", json={
                    "authority": grant.authority, "message_id": delivery["message_id"],
                    "event_id": event["event_id"], "lease_token": delivery["lease_token"],
                    "content_sha256": digest(delivery["content"]),
                    "reply_revision": delivery["reply_revision"]})
                approved = checked.status_code == 200
                assert checked.status_code in {200, 403, 409}, checked.text
            assert approved is (drift == "none")
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert not core.store.rows("SELECT 1 FROM outbox WHERE state='SENT'")
    run(scenario())
