"""Authenticated native delivery results inform only bounded public-source decisions."""

import json

import httpx
import pytest

from companion.contracts import CoreError
from companion.social_autonomy import SocialAutonomy, _sha
from companion.store import encode
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.social_owner_authority import close_provider_clients as close_provider_clients
from tests.test_life_expression_consumption import prepared, request


def recorded(core, tmp_path, outcome):
    job, _, authority, source = prepared(core, tmp_path)
    core.life_topics.consume(job, request(core, job, authority))
    row = core.store.one("SELECT * FROM outbox")
    content = json.loads(row["content_json"])
    module = SocialAutonomy(core.store, core.settings, core.model, core.clock)

    async def deliver():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as client:
            leased = await client.post("/internal/v1/deliveries/lease", json={
                "worker_id": "public-outcome-test", "authority": authority,
                "delivery_lane": "qq_account",
                "account_actions_authorization_sha256": content["authorization_sha256"],
            })
            assert leased.status_code == 200, leased.text
            item = leased.json()["items"][0]
            path = "/internal/v1/deliveries/" + item["message_id"]
            begun = await client.post(path + "/begin-send",
                                      json={"lease_token": item["lease_token"]})
            assert begun.status_code == 200, begun.text
            checked = await client.post("/internal/v1/account-actions/send-check", json={
                "authority": authority, "message_id": item["message_id"],
                "lease_token": item["lease_token"], "content_sha256": item["content_sha256"],
                "payload_sha256": content["payload_sha256"],
            })
            assert checked.status_code == 200, checked.text
            native = {"SUCCEEDED": "SENT", "REJECTED": "PERMANENT_FAILURE",
                      "UNKNOWN": "DELIVERY_UNCERTAIN"}
            result = await client.post(path + "/result", json={
                "lease_token": item["lease_token"], "result": native[outcome],
                "account_action_receipt": {
                    "message_id": item["message_id"], "lease_token": item["lease_token"],
                    "payload_sha256": content["payload_sha256"], "action_type": "qzone.publish",
                    "result": outcome, "native_retcode": {
                        "SUCCEEDED": 0, "REJECTED": 1400, "UNKNOWN": None,
                    }[outcome],
                    "native_receipt_sha256": "e" * 64 if outcome != "UNKNOWN" else None,
                    "resource_id": "synthetic-public-tid" if outcome == "SUCCEEDED" else None,
                },
            })
            assert result.status_code == 200, result.text

    run(deliver())
    return module, source


@pytest.mark.parametrize("outcome", ["SUCCEEDED", "REJECTED", "UNKNOWN"])
def test_http_receipt_informs_next_decision_or_blocks_uncertainty(core, tmp_path, outcome):
    module, source = recorded(core, tmp_path, outcome)
    context = module.context([source])
    receipts = context["execution_outcomes"]
    assert len(receipts) == 1 and receipts[0]["result"] == outcome
    before = core.store.rows("SELECT * FROM model_usage")
    if outcome == "UNKNOWN":
        with pytest.raises(CoreError, match="SOCIAL_DELIVERY_RECONCILIATION_REQUIRED"):
            run(module.plan("after-public-unknown", source_ids=[source]))
        assert core.store.rows("SELECT * FROM model_usage") == before
    else:
        seen = []

        def provider():
            seen.append(json.loads(core.model.calls[-1][0][-1]["content"]))
            core.model.activity = encode({"kind": "skip", "reason_code": "NO_ACTION"})

        core.model.callback = provider
        result = run(module.plan("after-public-result", source_ids=[source]))
        assert result["state"] == "SKIPPED"
        assert seen[0]["execution_outcomes"][0]["result"] == outcome
        assert "evt_test_1" not in encode(seen)
        assert len(core.store.rows("SELECT * FROM model_usage")) == len(before) + 1
    assert len(core.store.rows("SELECT * FROM outbox")) == 1


def test_receipt_with_stale_lease_cannot_inform_next_decision(core, tmp_path):
    module, source = recorded(core, tmp_path, "SUCCEEDED")
    assert len(module.context([source])["execution_outcomes"]) == 1
    row = core.store.one("SELECT * FROM social_autonomy_reservations")
    receipt = json.loads(row["receipt_json"])
    receipt["lease_token"] = "lease_previous_attempt"
    # Keep the receipt hash valid so this exercises the original native lease binding.
    core.store.execute("UPDATE social_autonomy_reservations SET receipt_json=?,receipt_sha256=? "
                       "WHERE message_id=?", (encode(receipt), _sha(receipt), row["message_id"]))
    assert module.context([source])["execution_outcomes"] == []
    assert len(core.store.rows("SELECT * FROM model_usage")) == 1
