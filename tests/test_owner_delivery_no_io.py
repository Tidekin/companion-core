"""Zero-send closure cannot settle or relabel an uncertain platform effect."""

from copy import deepcopy
from pathlib import Path

import pytest

import companion_delivery_protocol as wire
from companion import cumulative, delivery_reconciliation, owner_authority, owner_input, paid_budget
from companion.contracts import CoreError, DeliveryResult, LeaseRequest
from companion.owner_authority_protocol import AuthorityStop, OwnerGrantPolicy, OwnerSendCheck
from companion.privacy import ErasureLedger
from companion.transport import lease_deliveries, recover_deliveries
from companion_delivery_protocol import no_io
from tests.support.evaluation import work
from tests.support.owner_delivery_reconciliation import delivery_proof
from tests.support.owner_input import captured, inbound


def reserved(core, tmp_path, *, reserve_send=False, retained_lease=False):
    capture, key = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    core.ingest(inbound(core, capture), "native-reserved-no-send")
    assert work(core)
    item = lease_deliveries(core.store, core.settings, LeaseRequest(
        worker_id="bridge", authority=capture.authority), core.clock())["items"][0]
    check = OwnerSendCheck(authority=item["authority"], message_id=item["message_id"],
        event_id=item["origin_event_id"], content_sha256=item["content_sha256"],
        lease_token=item["lease_token"], reply_revision=item["reply_revision"])
    if reserve_send:
        with core.store.transaction():
            cumulative.admit_operation(core.store, core.settings, core.clock(), "send",
                item["message_id"], wire.digest(check.model_dump(mode="json")),
                logical=item["origin_event_id"])
    core.clock.advance(core.settings.lease_seconds + 1)
    recover_deliveries(core.store, core.clock())
    stopped = owner_authority.stop(core.store, core.settings, core.clock,
        AuthorityStop(operation_id="stop-native-reserved", authority=capture.authority))
    if retained_lease:
        # Historical supersession keeps the authentic unstarted lease as evidence.
        core.store.execute('UPDATE outbox SET lease_token=%s WHERE message_id=%s',
                           (item["lease_token"], item["message_id"]))
    proof = delivery_proof(core, capture, key, item, check,
        DeliveryResult(lease_token=item["lease_token"], result="DELIVERY_UNCERTAIN"), stopped)
    proof.update(attempt_state="RESERVED", original_result_sha256=None,
                 attempt_record_sha256=proof["reserved_record_sha256"])
    proof["signature"] = key.sign(wire.signing_payload(proof)).hex()
    return proof, key


def test_superseded_retained_lease_closes_with_original_signed_no_io_proof(core, tmp_path):
    proof, _ = reserved(core, tmp_path, retained_lease=True)
    before = core.store.one('SELECT * FROM outbox WHERE message_id=%s', (proof["message_id"],))
    fees = core.store.rows("SELECT * FROM model_usage")
    journal = Path(core.settings.paid_model.journal_path).read_bytes()
    result = delivery_reconciliation.prepare(core.store, core.settings, proof, core.clock())
    assert result["state"] == "NOT_SENT"
    assert result["receipt"]["quiescence"] == proof
    assert wire.digest(before["lease_token"]) == proof["lease_sha256"]
    assert core.store.one('SELECT * FROM outbox WHERE message_id=%s',
                          (proof["message_id"],)) == before
    assert delivery_reconciliation.prepare(core.store, core.settings, proof, core.clock()) == result
    assert core.store.rows("SELECT * FROM model_usage") == fees
    assert Path(core.settings.paid_model.journal_path).read_bytes() == journal


def test_zero_send_is_native_not_sent_and_preserves_costs_and_original_attempt(core, tmp_path):
    proof, _ = reserved(core, tmp_path)
    before = core.store.rows("SELECT * FROM model_usage")
    budget = Path(core.settings.paid_model.journal_path).read_bytes()
    result = delivery_reconciliation.prepare(core.store, core.settings, proof, core.clock())
    assert result["state"] == "NOT_SENT"
    receipt = no_io.receipt(result["receipt"])
    assert receipt["delivery"]["attempt_count"] == 0
    assert receipt["delivery"]["original_state"] == "CANCELLED"
    assert receipt["quiescence"] == proof
    assert delivery_reconciliation.prepare(core.store, core.settings, proof, core.clock()) == result
    assert core.store.one("SELECT COUNT(*) n FROM audit_log "
                          "WHERE action='delivery.not_sent'")["n"] == 1
    assert core.store.one((
        'SELECT attempt_count,lease_token,provider_message_id FROM outbox '
        'WHERE message_id=%s'
    ), (proof["message_id"],)) == {
        "attempt_count": 0, "lease_token": None, "provider_message_id": None}
    assert core.store.rows("SELECT * FROM model_usage") == before
    assert Path(core.settings.paid_model.journal_path).read_bytes() == budget
    assert not delivery_reconciliation.readiness(core.store, core.settings,
        proof["message_id"], now=core.clock())["prepared"]
    with pytest.raises(CoreError, match="DELIVERY_QUIESCENCE_REQUIRED"):
        delivery_reconciliation.resolve(core.store, core.settings, proof["message_id"], "SENT",
            core.clock(), operation_id="no-manual-sent", operation_sha256="f" * 64)


@pytest.mark.parametrize("change", [
    "io_started", "result", "attempt", "provider", "ack", "delivery_result", "content",
    "source", "signature", "restore", "different_proof", "cleared_without_erasure",
    "reactivated", "retained_lease_mismatch",
])
def test_zero_send_requires_every_original_no_io_fact(core, tmp_path, change):
    proof, key = reserved(core, tmp_path)
    if change == "different_proof":
        delivery_reconciliation.prepare(core.store, core.settings, proof, core.clock())
        proof["bridge_journal_head_sha256"] = "f" * 64
    elif change == "io_started":
        proof["attempt_state"] = "IO_STARTED"
    elif change == "result":
        proof["original_result_sha256"] = "e" * 64
    elif change == "attempt":
        core.store.execute('UPDATE outbox SET attempt_count=1 WHERE message_id=%s',
                           (proof["message_id"],))
    elif change == "provider":
        core.store.execute((
            "UPDATE outbox SET provider_message_id='provider-ack' WHERE "
            'message_id=%s'
        ),
                           (proof["message_id"],))
    elif change == "ack":
        core.store.execute("UPDATE outbox SET native_ack_json='{}' WHERE message_id=%s",
                           (proof["message_id"],))
    elif change == "delivery_result":
        core.store.audit("delivery.result", proof["message_id"], "delivery", core.clock(),
                         state="UNKNOWN")
    elif change == "retained_lease_mismatch":
        core.store.execute('UPDATE outbox SET lease_token=%s WHERE message_id=%s',
                           ("lease_" + "f" * 32, proof["message_id"]))
    elif change == "content":
        proof["content_sha256"] = "d" * 64
    elif change == "source":
        core.store.execute("UPDATE events SET body_json='{}' WHERE event_id=%s",
                           (proof["event_id"],))
    elif change == "restore":
        with core.store.transaction():
            owner_authority.restore(core.store)
    elif change == "cleared_without_erasure":
        core.store.execute((
            "UPDATE outbox SET content_json='{}',content_sha256='' WHERE "
            'message_id=%s'
        ), (proof["message_id"],))
    elif change == "reactivated":
        from tests.support.owner_authority import activate

        original = core.store.one("SELECT policy_json FROM owner_authority_revisions "
                                  "WHERE revision=1")
        activate(core, OwnerGrantPolicy.model_validate_json(original["policy_json"]),
                 revision=2, operation="reactivate-before-closure")
    proof["signature"] = key.sign(wire.signing_payload(proof)).hex()
    if change == "signature":
        proof["signature"] = "0" * 128
    before = deepcopy(core.store.rows("SELECT * FROM outbox"))
    budget = Path(core.settings.paid_model.journal_path).read_bytes()
    with pytest.raises(CoreError):
        delivery_reconciliation.prepare(core.store, core.settings, proof, core.clock())
    assert core.store.rows("SELECT * FROM outbox") == before
    assert Path(core.settings.paid_model.journal_path).read_bytes() == budget


def test_retained_independent_send_liability_blocks_zero_counter(core, tmp_path, monkeypatch):
    from tests.support import owner_input as test_owner_input
    from tests.support.cumulative_budget import policy

    original_prepare = test_owner_input.prepare

    def configure(core, directory):
        grant = original_prepare(core, directory)
        policy(core)
        return grant.model_copy(update={
            "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings))})

    monkeypatch.setattr(test_owner_input, "prepare", configure)
    proof, _ = reserved(core, tmp_path, reserve_send=True)
    before = Path(core.settings.paid_model.journal_path).read_bytes()
    view = paid_budget.journal(core.store, core.settings.paid_model, allow_settlement=True)
    assert view["cumulative"]["operations"]["send:" + wire.digest(proof["message_id"])][
        "state"] == "RESERVED"
    assert core.store.one('SELECT attempt_count FROM outbox WHERE message_id=%s',
                          (proof["message_id"],))["attempt_count"] == 0
    with pytest.raises(CoreError, match="DELIVERY_NOT_SENT_UNPROVEN"):
        delivery_reconciliation.prepare(core.store, core.settings, proof, core.clock())
    assert Path(core.settings.paid_model.journal_path).read_bytes() == before


@pytest.mark.parametrize("before_receipt", [False, True])
def test_erasure_retains_only_original_hashes_for_no_io_closure(core, tmp_path, before_receipt):
    proof, _ = reserved(core, tmp_path)
    first = None
    if not before_receipt:
        first = delivery_reconciliation.prepare(core.store, core.settings, proof, core.clock())
    with core.store.transaction(control_write=True):
        ErasureLedger(core.settings.erasure_ledger).expire_events(
            core.store, [proof["event_id"]], core.clock())
    result = delivery_reconciliation.prepare(core.store, core.settings, proof, core.clock())
    assert result["state"] == "NOT_SENT"
    if first:
        assert result == first
    assert core.store.one('SELECT content_json,content_sha256 FROM outbox WHERE message_id=%s',
                          (proof["message_id"],)) == {"content_json": "{}", "content_sha256": ""}
    assert result["receipt"]["delivery"]["content_sha256"] == proof["content_sha256"]


def test_non_dispatch_receipt_never_uses_manual_unknown_validator(core, tmp_path):
    proof, _ = reserved(core, tmp_path)
    result = delivery_reconciliation.prepare(core.store, core.settings, proof, core.clock())
    with pytest.raises(ValueError, match="DELIVERY_RESOLUTION"):
        wire.validate_receipt(result["receipt"])
    with pytest.raises(ValueError, match="DELIVERY_RESOLUTION_BINDING_INVALID"):
        wire.validate_delivery(result["receipt"]["delivery"])
    assert paid_budget.journal(core.store, core.settings.paid_model, allow_settlement=True)
