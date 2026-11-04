"""Static catalog approval binds the original native source and current purposes."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from companion import meme_service, owner_authority, owner_delivery, owner_input
from companion.api import create_app
from companion.contracts import CoreError, LeaseRequest
from companion.expression import ExpressionEngine, MemeOffer
from companion.owner_authority_protocol import AuthorityStop, OwnerCapture, OwnerSendCheck
from companion.transport import begin_send, lease_deliveries
from companion_delivery_protocol.authority import capture_signing_payload
from tests.support.owner_authority import activate
from tests.test_evaluation import work
from tests.test_owner_input import captured, inbound


def source(core, tmp_path, *, purposes=("chat", "meme", "expression")):
    capture, key = captured(core, tmp_path)
    core.settings.expression_enabled = True
    core.settings.expression_selection_probability = 1
    core.settings.meme_semantics_enabled = False
    core.settings.meme_discovery_enabled = False
    original = owner_authority.current(core.store, core.settings, core.clock)
    policy = original.policy.model_copy(update={
        "purposes": list(purposes), "capability_policies": {
            "meme": meme_service.policy_hash(core.settings)} if "meme" in purposes else {}})
    active, _ = activate(core, policy, revision=1, operation="static-meme-grant")
    value = capture.model_dump(mode="json", exclude={"signature"})
    value["authority"] = active["authority"]
    value["signature"] = key.sign(capture_signing_payload(value)).hex()
    capture = OwnerCapture.model_validate(value)
    offer = MemeOffer(offer_key="meme:" + capture.event_id, intent="celebrate", candidates=[{
        "candidate_id": "c" * 64, "description": "合成庆祝图片", "category": "celebrate"}])
    return capture, offer


def test_static_offer_api_keeps_signature_separate_and_freezes_retry(core, tmp_path):
    capture, offer = source(core, tmp_path)
    app = create_app(core.settings)
    app.state.core = core
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
    path = "/internal/v1/owner-authority/inputs/approve"
    body = {**capture.model_dump(mode="json"), "meme_offer": offer.model_dump()}
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    saved = json.loads(core.store.one("SELECT value FROM runtime_meta WHERE key=?",
        (owner_input.INPUT + capture.event_id,))["value"])
    assert saved["capture"] == capture.model_dump(mode="json")
    assert saved["meme_offer"] == offer.model_dump()
    assert client.post(path, json=body, headers=headers).json()["duplicate"]
    for changed in (None, {**offer.model_dump(), "candidates": []}):
        response = client.post(path, json={**body, "meme_offer": changed}, headers=headers)
        assert response.status_code == 409, response.text
    response = client.post("/internal/v1/inbox/events", json=inbound(core, capture).model_dump(
        mode="json"), headers={**headers, "X-Idempotency-Key": capture.event_id})
    assert response.status_code == 202, response.text
    assert ExpressionEngine(core.store, core.settings, core.clock)._offer(capture.event_id) == (
        True, offer)


@pytest.mark.parametrize("purposes", [("chat", "meme"), ("chat", "expression")])
def test_static_offer_requires_both_purposes(core, tmp_path, purposes):
    capture, offer = source(core, tmp_path, purposes=purposes)
    with pytest.raises(CoreError):
        owner_input.approve(core.store, core.settings, core.clock, capture, meme_offer=offer)
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                              (owner_input.INPUT + capture.event_id,))


@pytest.mark.parametrize("change", ["stop", "policy", "revision", "recall"])
def test_static_offer_cannot_outlive_source_or_current_permission(core, tmp_path, change):
    capture, offer = source(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture, meme_offer=offer)
    core.ingest(inbound(core, capture), capture.event_id)
    engine = ExpressionEngine(core.store, core.settings, core.clock)
    assert engine._offer(capture.event_id) == (True, offer)
    if change == "stop":
        owner_authority.stop(core.store, core.settings, core.clock,
            AuthorityStop(operation_id="stop-static", authority=capture.authority))
    elif change == "policy":
        core.settings.expression_selection_probability = 0.5
    elif change == "revision":
        core.store.execute("UPDATE conversations SET reply_revision=reply_revision+1")
    else:
        core.store.execute("UPDATE events SET redacted=1 WHERE event_id=?", (capture.event_id,))
    assert engine._offer(capture.event_id) == (True, None)


def test_adding_offer_to_an_already_approved_input_conflicts(core, tmp_path):
    capture, offer = source(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    with pytest.raises(CoreError, match="OWNER_INPUT_CONFLICT"):
        owner_input.approve(core.store, core.settings, core.clock, capture, meme_offer=offer)


@pytest.mark.parametrize("change", [None, "policy", "revoke"])
def test_image_send_checks_current_meme_permission(core, tmp_path, change):
    capture, offer = source(core, tmp_path)
    def reply(request):
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "合成庆祝回复⬡" + "c" * 64 + "⬡"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 2}})
    owner_input.approve(core.store, core.settings, core.clock, capture, meme_offer=offer)
    core.ingest(inbound(core, capture), capture.event_id)
    assert work(core, reply)
    item = lease_deliveries(core.store, core.settings, LeaseRequest(
        worker_id="bridge", authority=capture.authority), core.clock())["items"][0]
    assert item["content"].get("image"), item
    begin_send(core.store, item["message_id"], item["lease_token"], core.clock(),
               core.settings, clock=core.clock)
    check = OwnerSendCheck(authority=item["authority"], message_id=item["message_id"],
        event_id=item["origin_event_id"], content_sha256=item["content_sha256"],
        lease_token=item["lease_token"], reply_revision=item["reply_revision"])
    if change == "policy":
        core.settings.expression_cooldown_minutes += 1
    elif change == "revoke":
        owner_authority.stop(core.store, core.settings, core.clock,
            AuthorityStop(operation_id="revoke-meme", authority=capture.authority))
    if change:
        with pytest.raises(CoreError):
            owner_delivery.send_check(core.store, core.settings, core.clock, check)
    else:
        assert owner_delivery.send_check(core.store, core.settings, core.clock, check)["approved"]
