"""Original native delivery evidence for the existing owner-resolution journal."""

import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import companion_delivery_protocol as wire
from companion import owner_authority
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.privacy import reply_content_erased_by_memory
from companion_delivery_protocol.authority import AuthorityRef


def frozen(store, proof):
    ref = AuthorityRef.from_dict(proof["authority"])
    row = store.one("SELECT * FROM owner_authority_revisions WHERE grant_id=? AND revision=?",
                    (ref.grant_id, ref.revision))
    if (not row or owner_authority.reference(row) != ref or row["state"] != "ACTIVE"
        or row["policy_redacted"] or row["journal_id"] != proof["budget_journal_id"]
        or wire.digest(json.loads(row["policy_json"])) != ref.policy_sha256):
        raise ValueError("original native authority changed")
    policy = OwnerGrantPolicy.model_validate_json(row["policy_json"])
    if policy.capture is None:
        raise ValueError("original native capture identity missing")
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(policy.capture.public_key)).verify(
        bytes.fromhex(proof["signature"]), wire.signing_payload(proof))
    return row, policy


def stopped(store, proof, stop, *, current=False):
    original, _ = frozen(store, proof)
    ref = AuthorityRef.from_dict(proof["authority"])
    if (not stop or stop["state"] != "STOPPED"
        or stop["grant_id"] != ref.grant_id or stop["revision"] != ref.revision + 1
        or stop["predecessor_sha256"] != original["record_sha256"]
        or stop["policy_sha256"] != ref.policy_sha256
        or stop["restore_generation"] != ref.restore_generation
        or stop["record_sha256"] != proof["core_stop_record_sha256"]
        or current and owner_authority._meta(store, owner_authority.GENERATION)
        != ref.restore_generation):
        raise ValueError("original native STOP required")


def binding(store, proof, row, authority, recorded, *, not_sent=False):
    from companion.private_life import event_digest

    original, policy = frozen(store, proof)
    if proof["action_type"] in wire.ACCOUNT_ACTIONS:
        return account_binding(store, proof, row, authority, recorded, original, policy)
    if (not authority or authority.get("authority") != proof["authority"]
        or json.loads(row["authority_json"]) != proof["authority"]
        or any(authority.get(k) != proof[k] for k in ("message_id", "event_id", "action_type"))
        or row["origin_event_id"] != proof["event_id"]
        or row["action_type"] != proof["action_type"]
        or (row["attempt_count"] != 0 if not_sent else row["attempt_count"] < 1)
        or authority.get("delivery_key") != row["delivery_key"]):
        raise ValueError("original native delivery changed")
    event = store.one("SELECT * FROM events WHERE event_id=?", (proof["event_id"],))
    if (not event or not event["redacted"]
        and event_digest(event) != authority.get("event_sha256")):
        raise ValueError("original native source changed")
    content_hash = authority.get("content_sha256")
    if (not wire.hash_value(content_hash)
        or proof["content_sha256"] is not None and proof["content_sha256"] != content_hash):
        raise ValueError("original native content hash changed")
    if row["content_sha256"]:
        content = json.loads(row["content_json"])
        if (row["content_sha256"] != content_hash or wire.digest(content) != content_hash
            or wire.digest(content if proof["payload_kind"] == "content_sha256"
                           else content["text"]) != proof["payload_sha256"]):
            raise ValueError("original native content changed")
    elif not (store.one("SELECT 1 FROM job_sources s JOIN events e USING(event_id) "
                       "WHERE s.job_id=? AND e.redacted=1 AND EXISTS "
                       "(SELECT 1 FROM runtime_meta WHERE key='erased_event:'||e.event_id)",
                       (authority.get("job_id"),))
              or reply_content_erased_by_memory(store, row)):
        raise ValueError("unproved erased native delivery")
    from companion.voice_original_proof import validate_original_voice_binding

    saved = store.one("SELECT value FROM runtime_meta WHERE key=?",
                      ("voice_reply:" + row["message_id"],))
    given = proof.get("voice_check")
    job = (store.one("SELECT * FROM voice_jobs WHERE job_id=?", (given["job_id"],))
           if given else None)
    voice_policy = policy.capability_policies.get("voice")
    if given is not None and not voice_policy:
        raise ValueError("original native voice scope missing")
    validate_original_voice_binding(receipt=json.loads(saved["value"]) if saved else None,
        job=job, proof=proof, outbox=row, content_sha256=content_hash,
        original_scope={"authority": proof["authority"]}, voice_policy_sha256=voice_policy)
    token = row["lease_token"]
    if token:
        check = {"authority": proof["authority"], "message_id": row["message_id"],
                 "event_id": proof["event_id"], "content_sha256": content_hash,
                 "lease_token": token}
        if proof["reply_revision"] is not None:
            check["reply_revision"] = proof["reply_revision"]
        if given is not None:
            check["voice"] = given
        if (wire.digest(token) != proof["lease_sha256"]
            or wire.digest(check) != proof["send_check_sha256"]):
            raise ValueError("original native send fence changed")
    elif recorded is None and not not_sent:
        raise ValueError("original native send lease missing")
    delivery = {"message_id": row["message_id"], "event_id": row["origin_event_id"],
        "action_type": row["action_type"], "content_sha256": content_hash,
        "delivery_key_sha256": wire.digest(row["delivery_key"]),
        "authority_sha256": wire.digest(authority), "attempt_count": row["attempt_count"],
        "created_at": row["created_at"], "lease_until": row["lease_until"],
        "original_state": recorded["original_state"] if recorded else row["state"]}
    if not_sent:
        from companion_delivery_protocol import no_io

        no_io.delivery(delivery)
    else:
        wire.validate_delivery(delivery)
    if recorded is not None and recorded != delivery:
        raise ValueError("retained native delivery binding changed")
    return row, {"original": original, "policy": policy}, delivery


def account_binding(store, proof, row, authority, recorded, original, policy):
    from companion.account_original_proof import validate_original_account_send_check

    reservation = store.one("SELECT * FROM social_autonomy_reservations WHERE message_id=?",
                            (row["message_id"],))
    plan = store.one("SELECT * FROM social_autonomy_plans WHERE plan_id=?",
                    (proof["account_plan_id"],))
    check = validate_original_account_send_check(proof=proof, outbox=row, receipt=authority,
        plan=plan, reservation=reservation, original_authority=proof["authority"],
        original_policy=policy)
    if (reservation["action_type"] != proof["action_type"]
        or reservation["released_at"] is not None or reservation["dispatched_at"] is None
        or row["attempt_count"] < 1 or row["provider_message_id"] is not None
        or row["native_ack_json"] is not None
        or proof["authorization_sha256"] != policy.capability_policies["account_actions"]):
        raise ValueError("original native account attempt changed")
    receipt = json.loads(reservation["receipt_json"])
    if (reservation["receipt_sha256"] != (wire.digest(receipt) if receipt else None)
        or receipt and (receipt.get("result") != "UNKNOWN"
            or any(receipt.get(k) != v for k, v in {
                "message_id": row["message_id"], "action_type": row["action_type"],
                "payload_sha256": proof["payload_sha256"],
                "lease_token": check["lease_token"]}.items()))):
        raise ValueError("original native account receipt changed")
    delivery = {"message_id": row["message_id"], "event_id": row["origin_event_id"],
        "action_type": row["action_type"], "content_sha256": proof["content_sha256"],
        "delivery_key_sha256": wire.digest(row["delivery_key"]),
        "authority_sha256": wire.digest(authority), "attempt_count": row["attempt_count"],
        "created_at": row["created_at"], "lease_until": row["lease_until"],
        "original_state": recorded["original_state"] if recorded else row["state"],
        "account_plan_id": proof["account_plan_id"],
        "authorization_sha256": proof["authorization_sha256"],
        "payload_sha256": proof["payload_sha256"],
        "reservation_sha256": wire.digest({k: v for k, v in reservation.items()
                                           if k not in {"receipt_json", "receipt_sha256"}}),
        "account_receipt_sha256": reservation["receipt_sha256"]}
    wire.validate_delivery(delivery)
    if recorded is not None and recorded != delivery:
        raise ValueError("retained native account binding changed")
    return row, {"original": original, "policy": policy}, delivery
