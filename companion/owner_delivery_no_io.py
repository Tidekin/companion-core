"""Close a stopped native reservation only when Core never began its send."""

import companion_delivery_protocol as wire
from companion import owner_authority, owner_delivery_reconciliation, paid_budget
from companion.contracts import CoreError
from companion.store import encode
from companion_delivery_protocol import no_io

PREFIX = "owner_delivery_not_sent:"


def prepare(store, custody, proof, now):
    """The caller holds the existing reconciliation transaction; no quota is refunded."""
    from companion import delivery_reconciliation as recovery

    no_io.proof(proof)
    prior = recovery._meta(store, PREFIX + proof["message_id"])
    if prior is not None and prior["quiescence"] != proof:
        raise CoreError("DELIVERY_NOT_SENT_CONFLICT", 409)
    view = paid_budget.journal(store, custody, allow_settlement=True, initialize=False)
    stop = store.one('SELECT owner_authority_revisions.grant_id AS grant_id, '
        'owner_authority_revisions.revision AS revision, owner_authority_revisions.operation_id AS '
        'operation_id, owner_authority_revisions.request_sha256 AS request_sha256, '
        'owner_authority_revisions.state AS state, owner_authority_revisions.policy_json AS '
        'policy_json, owner_authority_revisions.policy_sha256 AS policy_sha256, '
        'owner_authority_revisions.policy_redacted AS policy_redacted, '
        'owner_authority_revisions.restore_generation AS restore_generation, '
        'owner_authority_revisions.predecessor_sha256 AS predecessor_sha256, '
        'owner_authority_revisions.record_sha256 AS record_sha256, '
        'owner_authority_revisions.journal_id AS journal_id, '
        'owner_authority_revisions.journal_path AS journal_path, '
        'owner_authority_revisions.created_at AS created_at FROM owner_authority_revisions AS '
        'owner_authority_revisions WHERE owner_authority_revisions.grant_id = %s AND '
        'owner_authority_revisions.record_sha256 = %s',
                     (proof["authority"]["grant_id"], proof["core_stop_record_sha256"]))
    owner_delivery_reconciliation.stopped(store, proof, stop, current=True, chain=True)
    if (prior is None and (not view["owner_authority"]
                          or view["owner_authority"]["record_sha256"] != stop["record_sha256"])
        or wire.timestamp(proof["quiesced_at"]) > wire.timestamp(now)):
        raise CoreError("DELIVERY_NOT_SENT_STOP_REQUIRED", 409)
    row = recovery._row(store, proof["message_id"])
    if (row["state"] != "CANCELLED" or row["attempt_count"] != 0
        or row["provider_message_id"] is not None
        or row["native_ack_json"] is not None
        or recovery._meta(store, recovery.PREPARED + proof["message_id"]) is not None
        or recovery._meta(store, recovery.PROJECTION + proof["message_id"]) is not None
        or proof["message_id"] in view["send_resolutions"]
        or "send:" + wire.digest(proof["message_id"]) in view["cumulative"]["operations"]
        or store.one("SELECT 1 FROM audit_log WHERE entity_id = %s AND action = 'delivery.result'",
                     (proof["message_id"],))
        or owner_authority._meta(store, owner_authority.GENERATION)
        != proof["authority"]["restore_generation"]):
        raise CoreError("DELIVERY_NOT_SENT_UNPROVEN", 409)
    # Supersession retains an unstarted lease as evidence. The original binding
    # validates any retained token against the signed lease and send-check hashes.
    _, _, original = owner_delivery_reconciliation.binding(
        store, proof, row, recovery._authority(store, proof["message_id"]),
        prior["delivery"] if prior else None, not_sent=True)
    if prior is not None:
        no_io.receipt(prior)
        if prior["delivery"] != original:
            raise CoreError("DELIVERY_NOT_SENT_CONFLICT", 409)
        return prior
    value = {"kind": no_io.KIND, "version": 1, "state": "NOT_SENT", "paused": True,
             "at": now, "delivery": original, "quiescence": proof}
    value["receipt_sha256"] = wire.digest(value)
    no_io.receipt(value)
    store.execute('INSERT INTO runtime_meta VALUES (%s, %s)',
                  (PREFIX + proof["message_id"], encode(value)))
    store.audit("delivery.not_sent", proof["message_id"], "bridge", now,
                receipt_sha256=value["receipt_sha256"], quiescence_sha256=wire.digest(proof))
    return value


def candidate(store, proof):
    """Only the zero-send native branch; other reservations keep the manual flow."""
    if (proof.get("version") != 2 or proof.get("attempt_state") != "RESERVED"
        or proof.get("original_result_sha256") is not None
        or proof.get("action_type") not in no_io.TEXT_ACTIONS):
        return False
    row = store.one('SELECT attempt_count FROM outbox WHERE message_id = '
        '%s', (proof["message_id"],))
    return bool(row and row["attempt_count"] == 0)
