"""Independent owner controls close uncertain deliveries without fabricating native ACKs."""

import pytest

from companion.store import encode, uid
from companion.transport import digest
from tests.conftest import ingest
from tests.support import owner_console as test_owner_console


@pytest.fixture
def client(core, monkeypatch):
    yield from test_owner_console.client.__wrapped__(core, monkeypatch)


@pytest.mark.parametrize("outcome", ["SENT", "CANCELLED"])
def test_owner_console_reconciles_native_poke_with_audit_and_no_retry(client, core, outcome):
    accepted = ingest(core, 1, "source")
    event = core.store.one('SELECT * FROM events WHERE event_id=%s', (accepted["event_id"],))
    message_id = uid("msg")
    content = {"kind": "poke", "target_user_id": core.settings.owner_qq_id}
    core.store.insert("outbox", message_id=message_id, origin_event_id=event["event_id"],
        conversation_id=event["conversation_id"], action_type="qq.private.poke",
        content_json=encode(content), content_sha256=digest(content), state="DELIVERY_UNCERTAIN",
        delivery_key="poke-response:" + accepted["job_id"], attempt_count=1,
        lease_token="lease_" + "a" * 32, created_at=core.clock(), updated_at=core.clock())
    path = "/v1/console/deliveries/" + message_id + "/reconcile"
    body = {"outcome": outcome}
    assert client.post(path, json=body, headers={
        "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value(),
    }).status_code == 401
    headers = test_owner_console.login(client)
    assert client.post(path, json=body, headers={
        "Origin": test_owner_console.ORIGIN,
    }).status_code == 403
    shown = client.get("/v1/console/diagnostics").json()["items"]
    projection = next(item for item in shown if item["id"] == message_id)
    assert projection["detail"]["can_reconcile"] is True
    before = core.store.rows("SELECT * FROM model_usage")
    result = client.post(path, json=body, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json() == {"state": outcome}
    assert client.post(path, json=body, headers=headers).json() == result.json()
    changed = "CANCELLED" if outcome == "SENT" else "SENT"
    assert client.post(path, json={"outcome": changed}, headers=headers).status_code == 409
    row = core.store.one('SELECT * FROM outbox WHERE message_id=%s', (message_id,))
    assert row["state"] == outcome and row["attempt_count"] == 1
    assert row["lease_token"] is row["native_ack_json"] is row["provider_message_id"] is None
    assert core.store.rows("SELECT * FROM model_usage") == before
    assert len(core.store.rows("SELECT * FROM outbox")) == 1
    assert len(core.store.rows((
        "SELECT * FROM audit_log WHERE action='delivery.owner_reconciled' AND"
        ' entity_id=%s'
    ), (message_id,))) == 1
