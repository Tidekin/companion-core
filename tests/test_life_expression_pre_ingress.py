"""Signed current owner input fences native Life channel checks before event ingress."""

import hashlib
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import paid_budget
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.qq_rich import input_digest
from companion_delivery_protocol.authority import capture_signing_payload
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.life_expression_consumption import (
    close_provider_clients as close_provider_clients,
)
from tests.support.life_expression_consumption import prepared
from tests.support.owner_authority import activate


@pytest.mark.parametrize("action,destination", [
    ("qq.private.proactive", "PRIVATE_MESSAGE_DRAFT"), ("qzone.publish", "QZONE_DRAFT"),
])
def test_signed_capture_preempts_final_automatic_channel_dispatch(core, tmp_path, action,
                                                                 destination):
    _, policy, _, _ = prepared(core, tmp_path, destination=destination, execute=False,
                               automatic_actions=[action])
    key = Ed25519PrivateKey.generate()
    identity = paid_budget.sha([core.settings.owner_qq_id, core.settings.bot_qq_id])
    policy = OwnerGrantPolicy.model_validate({**policy.model_dump(),
        "purposes": [*policy.purposes, "chat"],
        "capture": {"public_key": key.public_key().public_bytes_raw().hex(),
                    "identity_sha256": identity, "protocol_epoch": "life-final-fence"}})
    approved, _ = activate(core, policy, revision=1, operation="automatic-life-with-owner-ingress")
    authority = approved["authority"]
    job_id = core.life_topics.schedule()
    assert job_id and run(core.run_once(job_id=job_id))
    assert core.life_topics.consume_due(diary=core.diary)
    usage = core.store.rows("SELECT * FROM model_usage")

    async def scenario():
        async with real_core_http(core) as port:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                headers={"Authorization": "Bearer "
                         + core.settings.bridge_token.get_secret_value()}) as client:
                lease = {"worker_id": "synthetic-final-fence", "authority": authority}
                if action == "qzone.publish":
                    lease.update(delivery_lane="qq_account",
                        account_actions_authorization_sha256=(
                            core.settings.social_autonomy.authorization_sha256))
                leased = await client.post("/internal/v1/deliveries/lease", json=lease)
                assert leased.status_code == 200, leased.text
                item = leased.json()["items"][0]
                begun = await client.post(
                    "/internal/v1/deliveries/" + item["message_id"] + "/begin-send",
                    json={"lease_token": item["lease_token"]})
                assert begun.status_code == 200, begun.text
                source = (f"{core.settings.bot_qq_id}:onebot-direct:FriendMessage:"
                          f"{core.settings.owner_qq_id}:epoch:life-final-fence:123")
                capture = dict(authority=authority, kind="authenticated_onebot_ws",
                    source_event_id=source,
                    event_id="evt_" + hashlib.sha256(source.encode()).hexdigest(),
                    input_sha256=input_digest({"text": "先看我的新问题"}),
                    frame_sha256="b" * 64, evidence_sha256="c" * 64,
                    identity_sha256=identity, protocol_epoch="life-final-fence",
                    occurred_at=core.clock(), received_at=core.clock())
                capture["signature"] = key.sign(capture_signing_payload(capture)).hex()
                received = await client.post("/internal/v1/owner-authority/inputs/approve",
                                             json=capture)
                assert received.status_code == 200, received.text
                assert not core.store.one('SELECT 1 FROM events WHERE event_id=%s',
                                           (capture["event_id"],))
                check = {"authority": authority, "message_id": item["message_id"],
                         "lease_token": item["lease_token"],
                         "content_sha256": item["content_sha256"]}
                row = core.store.one('SELECT * FROM outbox WHERE message_id=%s',
                                      (item["message_id"],))
                if action == "qzone.publish":
                    check["payload_sha256"] = json.loads(row["content_json"])["payload_sha256"]
                    path = "/internal/v1/account-actions/send-check"
                else:
                    check["event_id"] = row["origin_event_id"]
                    path = "/internal/v1/owner-authority/send-check"
                denied = await client.post(path, json=check)
                assert denied.status_code == 409, denied.text
                assert core.store.one("SELECT state FROM outbox")["state"] != "SENT"
                assert core.store.rows("SELECT * FROM model_usage") == usage
                if action == "qzone.publish":
                    assert core.store.one(
                        "SELECT dispatched_at FROM social_autonomy_reservations",
                    ) == {"dispatched_at": None}
    run(scenario())
