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
    row = store.one('SELECT owner_authority_revisions.grant_id AS grant_id, '
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
        'owner_authority_revisions.revision = %s',
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


def stopped(store, proof, stop, *, current=False, chain=False):
    original, _ = frozen(store, proof)
    ref = AuthorityRef.from_dict(proof["authority"])
    if chain:
        from companion.owner_stop_receipt import stopped_chain

        if (not stop or stop["record_sha256"] != proof["core_stop_record_sha256"]
            or stopped_chain(store, ref, through=stop) is None):
            raise ValueError("original native STOP required")
        return
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
    event = store.one('SELECT events.event_id AS event_id, events.schema_version AS '
        'schema_version, events.source AS source, events.source_event_id AS source_event_id, '
        'events.conversation_id AS conversation_id, events.actor_id AS actor_id, events.event_type '
        'AS event_type, events.occurred_at AS occurred_at, events.ingested_at AS ingested_at, '
        'events.privacy_scope AS privacy_scope, events.body_json AS body_json, events.trace_id AS '
        'trace_id, events.idempotency_key AS idempotency_key, events.fingerprint AS fingerprint, '
        'events.redacted AS redacted, events.memory_processed AS memory_processed, '
        'events.reply_revision AS reply_revision FROM events AS events WHERE events.event_id = '
        '%s', (proof["event_id"],))
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
    elif not (store.one('SELECT 1 FROM job_sources AS s JOIN events AS e USING (event_id) WHERE '
        's.job_id = %s AND e.redacted = 1 AND EXISTS(SELECT 1 FROM runtime_meta WHERE key = '
        "'erased_event:' || e.event_id)",
                       (authority.get("job_id"),))
              or reply_content_erased_by_memory(store, row)):
        raise ValueError("unproved erased native delivery")
    from companion.voice_original_proof import validate_original_voice_binding

    saved = store.one('SELECT value FROM runtime_meta WHERE key = %s',
                      ("voice_reply:" + row["message_id"],))
    given = proof.get("voice_check")
    job = (store.one('SELECT voice_jobs.job_id AS job_id, voice_jobs.operation AS operation, '
        'voice_jobs.event_id AS event_id, voice_jobs.native_source_event_id AS '
        'native_source_event_id, voice_jobs.conversation_id AS conversation_id, '
        'voice_jobs.message_id AS message_id, voice_jobs.source_segment_index AS '
        'source_segment_index, voice_jobs.source_sha256 AS source_sha256, '
        'voice_jobs.source_input_sha256 AS source_input_sha256, voice_jobs.source_proof_sha256 AS '
        'source_proof_sha256, voice_jobs.identity_sha256 AS identity_sha256, '
        'voice_jobs.window_sha256 AS window_sha256, voice_jobs.captured_privacy_epoch AS '
        'captured_privacy_epoch, voice_jobs.captured_reply_revision AS captured_reply_revision, '
        'voice_jobs.outbox_content_sha256 AS outbox_content_sha256, voice_jobs.outbox_delivery_key '
        'AS outbox_delivery_key, voice_jobs.node_id AS node_id, voice_jobs.manifest_sha256 AS '
        'manifest_sha256, voice_jobs.config_sha256 AS config_sha256, voice_jobs.config_json AS '
        'config_json, voice_jobs.generation AS generation, voice_jobs.state AS state, '
        'voice_jobs.input_mime AS input_mime, voice_jobs.input_sha256 AS input_sha256, '
        'voice_jobs.input_bytes AS input_bytes, voice_jobs.input_duration_seconds AS '
        'input_duration_seconds, voice_jobs.source_partial AS source_partial, '
        'voice_jobs.input_blob AS input_blob, voice_jobs.result_blob AS result_blob, '
        'voice_jobs.result_sha256 AS result_sha256, voice_jobs.result_bytes AS result_bytes, '
        'voice_jobs.lease_token_sha256 AS lease_token_sha256, voice_jobs.lease_json AS lease_json, '
        'voice_jobs.lease_issued_at AS lease_issued_at, voice_jobs.lease_until AS lease_until, '
        'voice_jobs.started_at AS started_at, voice_jobs.completed_at AS completed_at, '
        'voice_jobs.created_at AS created_at, voice_jobs.updated_at AS updated_at, '
        'voice_jobs.deadline_at AS deadline_at, voice_jobs.result_expires_at AS result_expires_at, '
        'voice_jobs.last_error_code AS last_error_code, voice_jobs.stop_token_sha256 AS '
        'stop_token_sha256, voice_jobs.incarnation AS incarnation, voice_jobs.content_retired AS '
        'content_retired, voice_jobs.cleanup_state AS cleanup_state, voice_jobs.cleanup_epoch AS '
        'cleanup_epoch, voice_jobs.cleanup_confirmed_at AS cleanup_confirmed_at, '
        'voice_jobs.authority_json AS authority_json, voice_jobs.singing_context_json AS '
        'singing_context_json, voice_jobs.reserved_output_bytes AS reserved_output_bytes, '
        'voice_jobs.source_budget_at AS source_budget_at, voice_jobs.source_reserved_requests AS '
        'source_reserved_requests, voice_jobs.source_reserved_bytes AS source_reserved_bytes, '
        'voice_jobs.source_used_requests AS source_used_requests, voice_jobs.source_used_bytes AS '
        'source_used_bytes FROM voice_jobs AS voice_jobs WHERE voice_jobs.job_id = '
        '%s', (given["job_id"],))
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

    reservation = store.one('SELECT social_autonomy_reservations.plan_id AS plan_id, '
        'social_autonomy_reservations.message_id AS message_id, '
        'social_autonomy_reservations.action_type AS action_type, '
        'social_autonomy_reservations.semantic_sha256 AS semantic_sha256, '
        'social_autonomy_reservations.reserved_day AS reserved_day, '
        'social_autonomy_reservations.reserved_at AS reserved_at, '
        'social_autonomy_reservations.dispatched_at AS dispatched_at, '
        'social_autonomy_reservations.released_at AS released_at, '
        'social_autonomy_reservations.receipt_json AS receipt_json, '
        'social_autonomy_reservations.receipt_sha256 AS receipt_sha256 FROM '
        'social_autonomy_reservations AS social_autonomy_reservations WHERE '
        'social_autonomy_reservations.message_id = %s',
                            (row["message_id"],))
    plan = store.one('SELECT social_autonomy_plans.plan_id AS plan_id, '
        'social_autonomy_plans.idempotency_key AS idempotency_key, '
        'social_autonomy_plans.request_sha256 AS request_sha256, '
        'social_autonomy_plans.candidate_sha256 AS candidate_sha256, social_autonomy_plans.state '
        'AS state, social_autonomy_plans.policy_version AS policy_version, '
        'social_autonomy_plans.policy_sha256 AS policy_sha256, '
        'social_autonomy_plans.authorization_sha256 AS authorization_sha256, '
        'social_autonomy_plans.binding_sha256 AS binding_sha256, social_autonomy_plans.timezone AS '
        'timezone, social_autonomy_plans.policy_json AS policy_json, '
        'social_autonomy_plans.input_snapshot_json AS input_snapshot_json, '
        'social_autonomy_plans.source_snapshot_sha256 AS source_snapshot_sha256, '
        'social_autonomy_plans.plan_json AS plan_json, social_autonomy_plans.payload_sha256 AS '
        'payload_sha256, social_autonomy_plans.model_request_id AS model_request_id, '
        'social_autonomy_plans.last_error_code AS last_error_code, social_autonomy_plans.redacted '
        'AS redacted, social_autonomy_plans.created_at AS created_at, '
        'social_autonomy_plans.expires_at AS expires_at, social_autonomy_plans.updated_at AS '
        'updated_at FROM social_autonomy_plans AS social_autonomy_plans WHERE '
        'social_autonomy_plans.plan_id = %s',
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
