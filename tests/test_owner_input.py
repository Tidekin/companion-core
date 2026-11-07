"""Versioned signatures prove provenance without reviving stopped execution."""

import json

import pytest

from companion import owner_authority, owner_input
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop, OwnerGrantPolicy
from companion.store import encode
from tests.support.owner_authority import activate
from tests.support.owner_input import captured, inbound


def test_capture_admission_is_idempotent_and_signature_bound(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    assert owner_input.approve(core.store, core.settings, core.clock, capture) == {
        "approved": True, "duplicate": False}
    assert owner_input.approve(core.store, core.settings, core.clock, capture)["duplicate"]
    altered = capture.model_copy(update={"input_sha256": "f" * 64})
    with pytest.raises(CoreError, match="OWNER_CAPTURE_INVALID"):
        owner_input.approve(core.store, core.settings, core.clock, altered)


def test_history_rejects_future_capture_after_clock_rollback(core, tmp_path):
    from companion.store import after

    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    core.ingest(inbound(core, capture), "future-history-input")
    core.store.execute("UPDATE jobs SET state='CANCELLED'")
    original = owner_authority.current(core.store, core.settings, core.clock)
    policy = OwnerGrantPolicy.model_validate({**original.policy.model_dump(),
        "purposes": ["chat", "history"], "history": {"scope": "owner_private",
            "not_before": after(core.clock(), -3600), "max_turns": 8, "max_utf8_bytes": 8192}})
    activate(core, policy, revision=1, operation="history-before-clock-rollback")
    current = owner_authority.current(core.store, core.settings, core.clock)
    core.clock.advance(-1)
    with pytest.raises(CoreError, match="OWNER_SOURCE_CHANGED"):
        owner_input.source(core.store, core.settings, current, capture.event_id,
                            core.clock(), history=True)


def test_input_requires_admitted_capture_and_exact_content(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    message = inbound(core, capture)
    with pytest.raises(CoreError, match="OWNER_APPROVED_INPUT_REQUIRED"):
        owner_input.check_input(core.store, core.settings, core.clock, message)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    checked = owner_input.check_input(core.store, core.settings, core.clock, message)
    assert checked.authority.as_dict() == capture.authority
    changed = message.model_copy(update={
        "payload": message.payload.model_copy(update={"text": "changed"})})
    with pytest.raises(CoreError, match="OWNER_APPROVED_INPUT_REQUIRED"):
        owner_input.check_input(core.store, core.settings, core.clock, changed)


@pytest.mark.parametrize("boundary", ["stop", "restore", "revision", "expiry"])
def test_admitted_input_cannot_cross_runtime_authority_boundary(core, tmp_path, boundary):
    from companion.store import after

    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    message = inbound(core, capture)
    if boundary == "stop":
        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id="operation-stop-input", authority=capture.authority))
    elif boundary == "restore":
        owner_authority.restore(core.store)
    elif boundary == "revision":
        policy = owner_authority.current(core.store, core.settings, core.clock).policy
        activate(core, policy, revision=1, operation="operation-new-input-revision")
    else:
        core.clock = lambda: after(capture.received_at.isoformat(), 366 * 86400)
    with pytest.raises(CoreError):
        owner_input.check_input(core.store, core.settings, core.clock, message)


def test_native_ingest_binds_job_and_rejects_stopped_duplicate(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    message = inbound(core, capture)
    result = core.ingest(message, "owner-native-input")
    job = core.store.one("SELECT * FROM jobs WHERE job_id=%s", (result["job_id"],))
    assert json.loads(job["authority_json"]) == capture.authority
    assert core.ingest(message, "owner-native-input")["duplicate"]
    assert not core.store.rows("SELECT * FROM model_usage")
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="operation-stop-native-input", authority=capture.authority))
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_DISABLED"):
        core.ingest(message, "owner-native-input")


def test_bridge_api_capture_signature_required_before_native_ingest(core, tmp_path):
    from fastapi.testclient import TestClient

    from companion.api import create_app
    from companion.api_store import ApiStore

    capture, _ = captured(core, tmp_path)
    app = create_app(core.settings)
    app.state.core = core
    app.state.api_store = ApiStore(core.database, core.settings.state_directory)
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
    path = "/internal/v1/owner-authority/inputs/approve"
    assert client.post(path, json=capture.model_dump(mode="json")).status_code == 401
    invalid = capture.model_dump(mode="json")
    invalid["input_sha256"] = "0" * 64
    assert client.post(path, json=invalid, headers=headers).status_code == 403
    response = client.post(path, json=capture.model_dump(mode="json"), headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    response = client.post("/internal/v1/inbox/events", json=inbound(core, capture).model_dump(
        mode="json"), headers={**headers, "X-Idempotency-Key": "owner-api-native-input"})
    assert response.status_code == 202, response.text
    row = core.store.one("SELECT authority_json FROM jobs WHERE job_id=%s",
                         (response.json()["job_id"],))
    assert json.loads(row["authority_json"]) == capture.authority


def test_v1_domain_cannot_sign_v2_capture(core, tmp_path):
    capture, key = captured(core, tmp_path)
    value = capture.model_dump(mode="json", exclude={"signature"})
    signature = key.sign(b"companion-core/onebot-capture/v1\x00" + encode(value).encode()).hex()
    with pytest.raises(CoreError, match="OWNER_CAPTURE_INVALID"):
        owner_input.approve(core.store, core.settings, core.clock,
                            capture.model_copy(update={"signature": signature}))


@pytest.mark.parametrize("boundary", ["stop", "restore"])
def test_history_verification_does_not_reauthorize_capture(core, tmp_path, boundary):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    if boundary == "stop":
        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id="operation-stop-capture", authority=capture.authority))
    else:
        owner_authority.restore(core.store)
    assert owner_input.verify(core.store, core.settings, capture.model_dump(mode="json"))[0] == (
        capture)
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_DISABLED"):
        owner_input.approve(core.store, core.settings, core.clock, capture)


def test_erasure_removes_capture_and_prevents_its_readmission(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [capture.event_id], core.clock())
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key=%s",
                               (owner_input.INPUT + capture.event_id,))
    with pytest.raises(CoreError, match="EVENT_ERASED"):
        owner_input.approve(core.store, core.settings, core.clock, capture)


def test_explicit_history_scope_can_read_original_signed_source_after_revision(core, tmp_path):
    from tests.conftest import event

    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    inbound = event(core, text="synthetic owner input", origin="onebot-direct:FriendMessage:10001")
    inbound = inbound.model_copy(update={"event_id": capture.event_id,
                                          "source_event_id": capture.source_event_id})
    admitted = core.ingest(inbound, "synthetic-owner-source")
    with core.store.transaction():
        owner_authority.bind_job(core.store, core.settings, core.clock,
                                  admitted["job_id"], purpose="chat")
    original = owner_authority.current(core.store, core.settings, core.clock)
    assert owner_input.source(core.store, core.settings, original, capture.event_id,
                              core.clock())[1]["text"] == "synthetic owner input"
    core.store.execute("UPDATE jobs SET state='CANCELLED'")
    policy = OwnerGrantPolicy.model_validate({**original.policy.model_dump(),
        "purposes": ["chat", "history"], "history": {"scope": "owner_private",
            "not_before": core.clock(), "max_turns": 8, "max_utf8_bytes": 8192}})
    activate(core, policy, revision=1, operation="operation-history-revision")
    current = owner_authority.current(core.store, core.settings, core.clock)
    with pytest.raises(CoreError, match="OWNER_SOURCE_CHANGED"):
        owner_input.source(core.store, core.settings, current, capture.event_id, core.clock())
    assert owner_input.source(core.store, core.settings, current, capture.event_id,
                              core.clock(), history=True)[0]["event_id"] == capture.event_id
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [capture.event_id], core.clock())
    with pytest.raises(CoreError, match="OWNER_SOURCE_CHANGED"):
        owner_input.source(core.store, core.settings, current, capture.event_id,
                            core.clock(), history=True)
