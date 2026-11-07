"""Historical account evidence is checked after real native STOP without writes."""
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

import companion_delivery_protocol as wire
from companion import owner_authority
from companion.account_original_proof import validate_original_account_send_check
from companion.api import create_app
from companion.api_store import ApiStore
from companion.social_autonomy import erase_social_autonomy
from tests.postgres_fixture import logical_state
from tests.support.account_owner_transport import prepared
from tests.support.social_owner_authority import (
    close_provider_clients,  # noqa: F401 - shared native planner fixture
    stop,
)


def records(core, tmp_path):
    authority, frozen = prepared(core, tmp_path)
    app = create_app(core.settings)
    app.state.core = core
    app.state.api_store = ApiStore(core.database, core.settings.state_directory)
    client = TestClient(app, headers={"Authorization": "Bearer " +
                                     core.settings.bridge_token.get_secret_value()})
    try:
        response = client.post("/internal/v1/deliveries/lease", json={
            "worker_id": "original-account-proof", "authority": authority,
            "delivery_lane": "qq_account",
            "account_actions_authorization_sha256": frozen.authorization_sha256})
        assert response.status_code == 200, response.text
        assert response.json()["items"][0]["message_id"] == frozen.message_id
    finally:
        client.close()
    row = core.store.one('SELECT * FROM outbox WHERE message_id=%s', (frozen.message_id,))
    receipt = json.loads(core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
        ("account_action_delivery:" + frozen.message_id,))["value"])
    check = {key: receipt[key] for key in (
        "authority", "message_id", "lease_token", "content_sha256", "payload_sha256")}
    proof = {"authority": authority, "message_id": frozen.message_id,
        "account_plan_id": frozen.plan_id, "action_type": frozen.action_type,
        "payload_kind": "account_payload_sha256", "content_sha256": receipt["content_sha256"],
        "payload_sha256": receipt["payload_sha256"],
        "lease_sha256": wire.digest(row["lease_token"]),
        "send_check_sha256": wire.digest(check)}
    stop(core, authority)
    policy = owner_authority.evidence(core.store, core.settings, authority).policy
    return dict(proof=proof, outbox=row, receipt=receipt,
        plan=core.store.one('SELECT * FROM social_autonomy_plans WHERE plan_id=%s',
                            (frozen.plan_id,)),
        reservation=core.store.one('SELECT * FROM social_autonomy_reservations WHERE message_id=%s',
                                    (frozen.message_id,)),
        original_authority=authority, original_policy=policy), check


def test_original_account_check_survives_stop_expiry_and_current_config_change(core, tmp_path):
    values, expected = records(core, tmp_path)
    before = logical_state(core.store)
    core.clock.advance(86400)
    core.settings.social_autonomy = core.settings.social_autonomy.model_copy(
        update={"enabled": False})
    assert validate_original_account_send_check(**values) == expected
    assert logical_state(core.store) == before


@pytest.mark.parametrize("change", ["lease", "hash", "authority", "plan", "private_shape"])
def test_original_account_check_rejects_changed_binding(core, tmp_path, change):
    values, _ = records(core, tmp_path)
    values = deepcopy(values)
    if change == "lease":
        values["receipt"]["lease_token"] += "x"
    elif change == "hash":
        values["proof"]["send_check_sha256"] = "0" * 64
    elif change == "authority":
        values["original_authority"]["revision"] += 1
    elif change == "plan":
        values["plan"]["payload_sha256"] = "0" * 64
    else:
        values["proof"]["event_id"] = "evt_unrelated"
    with pytest.raises(ValueError, match="ORIGINAL_ACCOUNT_SEND_CHECK_CHANGED"):
        validate_original_account_send_check(**values)


def test_original_account_check_uses_retained_hashes_after_source_erasure(core, tmp_path):
    values, expected = records(core, tmp_path)
    sources = [row["event_id"] for row in core.store.rows(
        'SELECT event_id FROM social_autonomy_sources WHERE plan_id=%s',
        (values["plan"]["plan_id"],))]
    with core.store.transaction():
        erase_social_autonomy(core.store, sources)
    values["outbox"] = core.store.one('SELECT * FROM outbox WHERE message_id=%s',
                                       (expected["message_id"],))
    values["plan"] = core.store.one('SELECT * FROM social_autonomy_plans WHERE plan_id=%s',
                                     (values["plan"]["plan_id"],))
    assert values["plan"]["redacted"] == 1
    before = logical_state(core.store)
    assert validate_original_account_send_check(**values) == expected
    assert logical_state(core.store) == before
