"""V2 withdrawal preserves native ledger semantics across execution boundaries."""


import pytest

from companion import owner_authority, owner_input, owner_recall
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from tests.support.owner_input import captured, inbound
from tests.support.owner_recall import receipt


@pytest.mark.parametrize("boundary", ["active", "stop", "restore"])
def test_recall_uses_original_evidence_after_execution_is_disabled(core, tmp_path, boundary):
    capture, key = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    core.ingest(inbound(core, capture), "recall-input")
    body = receipt(core, capture, key)
    if boundary == "stop":
        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id="stop-before-recall", authority=capture.authority))
    elif boundary == "restore":
        owner_authority.restore(core.store)
    result = owner_recall.receive(core.store, core.settings, core.ledger, core.clock, body)
    assert result["state"] == "RECALLED"
    assert not core.store.one('SELECT 1 FROM runtime_meta WHERE key=%s',
                              (owner_input.INPUT + capture.event_id,))
    assert core.store.one('SELECT redacted FROM events WHERE event_id=%s',
                           (capture.event_id,))["redacted"]
    repeated = owner_recall.receive(core.store, core.settings, core.ledger, core.clock, body)
    assert repeated["duplicate"]
    with pytest.raises(CoreError):
        core.ingest(inbound(core, capture), "recall-input")


def test_recall_before_capture_closes_source_before_inbox_ingestion(core, tmp_path):
    capture, key = captured(core, tmp_path)
    body = receipt(core, capture, key)
    assert owner_recall.receive(core.store, core.settings, core.ledger, core.clock, body)[
        "state"] == "PENDING_TARGET"
    owner_input.approve(core.store, core.settings, core.clock, capture)
    with pytest.raises(CoreError, match="EVENT_ERASED"):
        core.ingest(inbound(core, capture), "pending-recall-input")
    assert not core.store.one('SELECT 1 FROM events WHERE event_id=%s', (capture.event_id,))
    assert not core.store.one('SELECT 1 FROM jobs WHERE event_id=%s', (capture.event_id,))
    assert core.store.one('SELECT 1 FROM runtime_meta WHERE key=%s',
                          ("erased_event:" + capture.event_id,))


@pytest.mark.parametrize("change", ["signature", "source", "epoch", "identity", "future", "age"])
def test_recall_rejects_invalid_evidence_without_erasing(core, tmp_path, change):
    capture, key = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    core.ingest(inbound(core, capture), "recall-invalid-input")
    from companion.store import after

    changes = {"source": {"target_source_event_id": capture.source_event_id + "4"},
               "epoch": {"protocol_epoch": "different-epoch"},
               "identity": {"identity_sha256": "0" * 64},
               "age": {"received_at": after(core.clock(), 31)},
               "future": {"received_at": after(core.clock(), 3)}}
    body = receipt(core, capture, key, **changes.get(change, {}))
    if change == "signature":
        body = body.model_copy(update={"signature": "0" * 128})
    with pytest.raises(CoreError, match="OWNER_RECALL_INVALID"):
        owner_recall.receive(core.store, core.settings, core.ledger, core.clock, body)
    assert not core.store.one('SELECT redacted FROM events WHERE event_id=%s',
                               (capture.event_id,))["redacted"]


def test_http_recall_requires_bridge_auth_and_v2_signature(core, tmp_path):
    from fastapi.testclient import TestClient

    from companion.api import create_app
    from companion.api_store import ApiStore
    from companion.store import encode

    capture, key = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    core.ingest(inbound(core, capture), "recall-http-input")
    body = receipt(core, capture, key).model_dump(mode="json")
    app = create_app(core.settings)
    app.state.core = core
    app.state.api_store = ApiStore(core.database, core.settings.state_directory)
    client = TestClient(app)
    path = "/internal/v1/owner-authority/inputs/recall"
    headers = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
    assert client.post(path, json=body).status_code == 401
    legacy = key.sign(b"companion-core/onebot-owner-recall/v1\x00" + encode(
        {k: v for k, v in body.items() if k != "signature"}).encode()).hex()
    assert client.post(path, json={**body, "signature": legacy}, headers=headers).status_code == 403
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["state"] == "RECALLED"


def test_outgoing_provider_id_conflict_cannot_establish_inbound_direction(core, tmp_path):
    from companion.transport import enqueue

    capture, key = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    result = core.ingest(inbound(core, capture), "recall-conflicting-direction")
    event = core.store.one('SELECT * FROM events WHERE event_id=%s', (capture.event_id,))
    with core.store.transaction():
        message_id = enqueue(core.store, capture.event_id, event["conversation_id"],
                             "earlier outgoing", core.clock(), key="conflicting-provider-id")
        core.store.execute((
            "UPDATE outbox SET state='SENT',provider_message_id='123' WHERE "
            'message_id=%s'
        ), (message_id,))
    assert owner_recall.receive(core.store, core.settings, core.ledger, core.clock,
                               receipt(core, capture, key))["state"] == "PENDING_TARGET"
    assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (result["job_id"],))[
        "state"] == "QUEUED"
