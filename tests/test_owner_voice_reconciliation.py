"""Native voice retains original model, node, lease and retirement evidence at STOP."""

import pytest

from companion import delivery_reconciliation as reconciliation
from companion import owner_authority, owner_delivery
from companion.contracts import DeliveryResult, LeaseRequest
from companion.owner_authority_protocol import AuthorityStop, OwnerSendCheck
from companion.transport import begin_send, delivery_result, lease_deliveries
from companion.voice_jobs import VoiceJobs
from companion.voice_protocol import VoiceDeliveryEnvelope
from tests.support.evaluation import work
from tests.support.owner_delivery_reconciliation import delivery_proof
from tests.test_voice_flow import delivery_check, finish_node
from tests.voice_owner_fixtures import native_voice


@pytest.mark.parametrize("erase", [False, True])
def test_native_voice_unknown_uses_original_full_or_retired_proof(core, tmp_path, erase):
    capture, key, _ = native_voice(core, tmp_path, record=False)
    assert work(core)
    request = LeaseRequest(worker_id="voice-bridge", authority=capture.authority)
    assert not lease_deliveries(core.store, core.settings, request, core.clock())["items"]
    finish_node(VoiceJobs(core.store, core.settings, core.clock))
    item = lease_deliveries(core.store, core.settings, request, core.clock())["items"][0]
    envelope = VoiceDeliveryEnvelope.model_validate(item["voice"])
    check = OwnerSendCheck(authority=capture.authority, event_id=capture.event_id,
        message_id=item["message_id"], content_sha256=item["content_sha256"],
        lease_token=item["lease_token"], reply_revision=item["reply_revision"],
        voice=delivery_check(envelope))
    begin_send(core.store, item["message_id"], item["lease_token"], core.clock(),
               core.settings, clock=core.clock)
    owner_delivery.send_check(core.store, core.settings, core.clock, check)
    result = DeliveryResult(lease_token=item["lease_token"], result="DELIVERY_UNCERTAIN")
    delivery_result(core.store, core.settings, item["message_id"], result, core.clock())
    stopped = owner_authority.stop(core.store, core.settings, core.clock,
        AuthorityStop(operation_id="stop-native-voice-unknown", authority=capture.authority))
    proof = delivery_proof(core, capture, key, item, check, result, stopped)
    before = core.store.rows("SELECT * FROM model_usage")
    if erase:
        with core.store.transaction():
            core.ledger.expire_events(core.store, [capture.event_id], core.clock())
    prepared = reconciliation.prepare(core.store, core.settings, proof, core.clock())
    assert prepared["state"] == "READY"
    resolution = reconciliation.resolve(core.store, core.settings, item["message_id"], "CANCELLED",
        core.clock(), operation_id="native-voice-owner-decision", operation_sha256="a" * 64)
    assert resolution["receipt"]["quiescence"] == proof
    assert core.store.rows("SELECT * FROM model_usage") == before
