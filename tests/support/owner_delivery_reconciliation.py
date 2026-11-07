import companion_delivery_protocol as wire
from companion import owner_authority, owner_delivery, owner_input
from companion.contracts import DeliveryResult, LeaseRequest
from companion.owner_authority_protocol import AuthorityStop, OwnerSendCheck
from companion.transport import begin_send, delivery_result, lease_deliveries
from tests.support.evaluation import work
from tests.support.owner_input import captured, inbound


def pending(core, tmp_path):
    capture, key = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    core.ingest(inbound(core, capture), "native-unknown-source")
    assert work(core)
    item = lease_deliveries(core.store, core.settings, LeaseRequest(
        worker_id="bridge", authority=capture.authority), core.clock())["items"][0]
    check = OwnerSendCheck(authority=item["authority"], message_id=item["message_id"],
        event_id=item["origin_event_id"], content_sha256=item["content_sha256"],
        lease_token=item["lease_token"], reply_revision=item["reply_revision"])
    begin_send(core.store, item["message_id"], item["lease_token"], core.clock(),
               core.settings, clock=core.clock)
    owner_delivery.send_check(core.store, core.settings, core.clock, check)
    result = DeliveryResult(lease_token=item["lease_token"], result="DELIVERY_UNCERTAIN")
    delivery_result(core.store, core.settings, item["message_id"], result, core.clock())
    stopped = owner_authority.stop(core.store, core.settings, core.clock,
        AuthorityStop(operation_id="stop-unknown-native", authority=capture.authority))
    return delivery_proof(core, capture, key, item, check, result, stopped), key


def delivery_proof(core, capture, key, item, check, result, stopped):
    proof = {"kind": "owner_delivery_quiescence_v2", "version": 2,
        "authority": capture.authority,
        "budget_journal_id": core.store.one(
            "SELECT journal_id FROM owner_authority_revisions WHERE revision=1")["journal_id"],
        "message_id": item["message_id"], "event_id": capture.event_id,
        "action_type": item["action_type"], "fence": 1, "item_sha256": wire.digest(item),
        "content_sha256": item["content_sha256"],
        "payload_sha256": wire.digest(item["content"]["text"]),
        "payload_kind": "reply_sha256", "lease_sha256": wire.digest(item["lease_token"]),
        "send_check_sha256": wire.digest(check.model_dump(mode="json")),
        "reply_revision": item["reply_revision"], "attempt_state": "UNKNOWN",
        "attempt_record_sha256": "1" * 64, "reserved_record_sha256": "2" * 64,
        "stop_record_sha256": "3" * 64, "core_stop_record_sha256": stopped["record_sha256"],
        "bridge_journal_head_sha256": "4" * 64,
        "original_result_sha256": wire.digest(result.model_dump(mode="json")),
        "stopped_at": core.clock(), "quiesced_at": core.clock(),
        **({"voice_check": check.voice} if check.voice is not None else {})}
    proof["signature"] = key.sign(wire.signing_payload(proof)).hex()
    return proof
