"""Settlement of an owner text delivery whose guarded Core preflight exhausted its lease."""

import json
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field

import companion_delivery_protocol as wire
from companion import cumulative, live_text, owner_authority
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityReference
from companion_delivery_protocol import preflight


class ExpiredPreflight(AuthorityReference):
    kind: Literal["owner_reply_preflight_expired_v1"]
    message_id: str = Field(pattern=r"^msg_[a-f0-9]{32}$")
    stage: Literal["begin-send", "send-check"]
    item_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    lease_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    lease_until: str = Field(min_length=20, max_length=48)
    reserved_record_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    signature: str = Field(pattern=r"^[a-f0-9]{128}$")


def settle(store, settings, clock, body):
    """Consume only the exclusive Bridge's signed RESERVED witness for our last lease."""
    proof = body.model_dump(mode="json")
    key = "input_cancel_before_send:" + body.message_id
    with store.transaction(delivery_preflight=True):
        view = owner_authority.evidence(store, settings, body.authority)
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(view.policy.capture.public_key)).verify(
                bytes.fromhex(body.signature), preflight.signing_bytes(proof))
        except (AttributeError, ValueError, InvalidSignature):
            raise CoreError("INPUT_CANCEL_BEFORE_SEND_UNPROVEN", 403) from None
        receipt = {"cancelled": True, "message_id": body.message_id,
                   "state": "CANCELLED", "send_attempt_retained": True}
        prior = live_text._meta(store, key)
        if prior is not None:
            if prior != proof:
                raise CoreError("INPUT_CANCEL_RECEIPT_CONFLICT", 409)
            return receipt
        latest = owner_authority._latest(store)
        if (not latest or latest["state"] != "ACTIVE"
            or owner_authority.reference(latest).as_dict() != body.authority):
            raise CoreError("OWNER_PREFLIGHT_AUTHORITY_CHANGED", 409)
        lease = live_text._meta(store, "owner_preflight_lease:" + body.message_id)
        row = store.one("SELECT * FROM outbox WHERE message_id=?", (body.message_id,))
        # A begin-send ACK can be lost after Core commits SENDING. The original
        # exclusive Bridge still attests RESERVED and no provider dispatch, so
        # retain the actual begun count rather than treating the ACK as an effect.
        begun = body.stage == "send-check" or bool(row and row["attempt_count"] == 1)
        states = {"SENDING", "DELIVERY_UNCERTAIN"} if begun else {"LEASED", "PENDING", "CANCELLED"}
        if (lease != {k: proof[k] for k in ("item_sha256", "lease_sha256", "lease_until")}
            or not row or not row["authority_json"]
            or json.loads(row["authority_json"]) != body.authority
            or row["state"] not in states or row["attempt_count"] != int(begun)
            or row["provider_message_id"] is not None or row["native_ack_json"] is not None
            or (row["state"] == "DELIVERY_UNCERTAIN"
                and row["last_error_code"] != "SEND_LEASE_EXPIRED")
            or row["lease_token"] and wire.digest(row["lease_token"]) != body.lease_sha256
            or store.one("SELECT 1 FROM audit_log WHERE entity_id=? AND action='delivery.result'",
                         (body.message_id,))
            or wire.timestamp(clock()) < wire.timestamp(body.lease_until)):
            raise CoreError("INPUT_CANCEL_BEFORE_SEND_UNPROVEN", 409)
        now = clock()
        cumulative.send_result(store, body.message_id, "FAILED", now)
        # New owner input can already have cancelled an unstarted lease. Its exact
        # signed reservation still closes Bridge custody; retain Core's terminal reason.
        if row["state"] != "CANCELLED":
            store.execute("UPDATE outbox SET state='CANCELLED',"
                          "last_error_code='REPLY_NOT_DISPATCHED',updated_at=? WHERE message_id=?",
                          (now, body.message_id))
        store.execute("UPDATE outbox SET state='CANCELLED',lease_token=NULL,updated_at=?,"
                      "last_error_code='REPLY_SEGMENT_CHAIN_STOPPED' WHERE reply_group_id=? "
                      "AND segment_index>? AND state IN ('PENDING','RETRY_WAIT','LEASED')",
                      (now, row["reply_group_id"], row["segment_index"]))
        store.execute("DELETE FROM runtime_meta WHERE key=?",
                      ("owner_preflight_lease:" + body.message_id,))
        live_text._put(store, key, proof)
        store.audit("delivery.preflight_expired", body.message_id, "bridge", now,
                    proof_sha256=wire.digest(proof), stage=body.stage)
        return receipt
