"""Owner-requested copies of a positively unstarted, frozen text-reply suffix.

Original deliveries remain terminal. A separate console producer owns new Outbox
identities; it neither reruns the original model nor renews its execution grant.
"""

import json
from contextlib import nullcontext

from cryptography.exceptions import InvalidSignature
from pydantic import Field

from companion import owner_authority, owner_delivery, owner_input, reply_revision, transport
from companion.contracts import CoreError, StrictModel
from companion.privacy import epoch
from companion.store import encode, uid
from companion_delivery_protocol import MAX_INPUT_DELIVERIES, digest, no_io, preflight

KIND = "reply_recovery"
RECORD = "owner_reply_recovery:"
ORIGINAL = "owner_reply_recovery_original:"


class RecoveryRequest(StrictModel):
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def _meta(store, key):
    row = store.one("SELECT value FROM runtime_meta WHERE key=?", (key,))
    return json.loads(row["value"]) if row else None


def is_delivery(store, message_id):
    return bool(
        store.one(
            "SELECT 1 FROM outbox o JOIN jobs j ON j.event_id=o.origin_event_id "
            "WHERE o.message_id=? AND j.kind=?",
            (message_id, KIND),
        )
    )


def _fail():
    raise CoreError("REPLY_RECOVERY_NOT_PROVEN", 409)


def _original(store, settings, clock, message_id):
    from companion import runtime_config
    from companion.transport import reply_group

    group = reply_group(store, message_id)
    first = group["rows"][0]
    job = owner_delivery._job(store, first["reply_group_id"])
    if (
        not group["segmented"]
        or not 2 <= len(group["rows"]) <= 100
        or not job
        or job["kind"] != "chat"
        or job["state"] != "SUCCEEDED"
        or first["action_type"] != "qq.private.reply"
        or any(set(json.loads(r["content_json"])) != {"text"} for r in group["rows"])
    ):
        _fail()
    original = owner_authority.evidence(store, settings, json.loads(job["authority_json"]))
    current = owner_authority.current(store, settings, clock, purpose="chat")
    if (
        original.policy.owner_qq_id != current.policy.owner_qq_id
        or original.authority.grant_id != current.authority.grant_id
        or original.authority.restore_generation != current.authority.restore_generation
        or original.policy.capture != current.policy.capture
    ):
        _fail()
    reply_revision.check_sources(store, job["job_id"])
    runtime_config.get(store, settings, "job:" + job["job_id"])
    # The original signed capture remains evidence, while the current grant must
    # independently permit reading that historical private source.
    owner_input._source(
        store,
        settings,
        original,
        job["event_id"],
        clock(),
        allow_commands=False,
        verify_capture=lambda v: owner_input.verify(store, settings, v),
    )
    owner_input.source(store, settings, current, job["event_id"], clock(), history=True)
    snapshots = owner_delivery._snapshots(store, job)
    if any(
        p["kind"] in {"reader", "images", "singing", "video_preview_reply"} for p in snapshots
    ) or store.one("SELECT 1 FROM agent_image_inputs WHERE job_id=?", (job["job_id"],)):
        _fail()
    proof = owner_delivery._history_proof(store, job, snapshots)
    if proof != owner_input.history_proof(
        owner_input.history_for_owner(store, settings, clock, job, current)
    ):
        raise CoreError(reply_revision.CANCELLED, 409)
    owner_delivery._memories(store, settings, clock, current, snapshots)
    if len(group["text"]) > current.policy.max_reply_chars:
        _fail()
    requests = owner_delivery._usage(store, job, original, snapshots, message_id, group=group)
    context_hashes = owner_delivery._context_hashes(proof, snapshots)
    for row in group["rows"]:
        saved = _meta(store, owner_delivery.DELIVERY + row["message_id"])
        if saved != owner_delivery._material(store, row, job, original, requests, context_hashes):
            _fail()
    return group, job, current


def _unstarted(store, settings, row):
    """A retired lease needs positive native evidence; never infer no I/O from age."""
    from companion import owner_delivery_reconciliation

    message_id = row["message_id"]
    if (
        row["state"] != "CANCELLED"
        or row["provider_message_id"] is not None
        or row["native_ack_json"] is not None
        or row["sent_at"] is not None
        or store.one(
            "SELECT 1 FROM audit_log WHERE entity_id=? AND action='delivery.result'", (message_id,)
        )
    ):
        _fail()
    receipt = _meta(store, "owner_delivery_not_sent:" + message_id)
    expired = _meta(store, "input_cancel_before_send:" + message_id)
    if receipt is not None:
        no_io.receipt(receipt)
        proof = receipt["quiescence"]
        owner_delivery_reconciliation.binding(
            store,
            proof,
            row,
            _meta(store, owner_delivery.DELIVERY + message_id),
            receipt["delivery"],
            not_sent=True,
        )
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        from companion_delivery_protocol import signing_payload

        original = owner_authority.evidence(store, settings, proof["authority"])
        Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(original.policy.capture.public_key)
        ).verify(bytes.fromhex(proof["signature"]), signing_payload(proof))
        return
    if expired is not None and expired.get("kind") == preflight.KIND:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        original = owner_authority.evidence(store, settings, expired["authority"])
        Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(original.policy.capture.public_key)
        ).verify(bytes.fromhex(expired["signature"]), preflight.signing_bytes(expired))
        if (
            expired["message_id"] != message_id
            or expired["authority"] != json.loads(row["authority_json"])
            or row["last_error_code"] != "REPLY_NOT_DISPATCHED"
        ):
            _fail()
        return
    # A never-leased tail has no Bridge reservation or send admission to retire.
    if (
        row["attempt_count"] != 0
        or row["lease_token"] is not None
        or row["lease_until"] is not None
        or _meta(store, "owner_preflight_lease:" + message_id) is not None
        or "send:" + digest(message_id)
        in owner_authority.custody(store, settings, settlement=True)["cumulative"]["operations"]
    ):
        _fail()


def _selection(store, settings, clock, message_id):
    try:
        return _select(store, settings, clock, message_id)
    except (KeyError, TypeError, ValueError, InvalidSignature):
        _fail()


def _select(store, settings, clock, message_id):
    group, job, current = _original(store, settings, clock, message_id)
    prefix = 0
    for row in group["rows"]:
        if row["state"] != "SENT":
            break
        if (
            not row["sent_at"]
            or row["attempt_count"] < 1
            or not (row["provider_message_id"] or row["native_ack_json"])
        ):
            _fail()
        prefix += 1
    if not 0 < prefix < len(group["rows"]):
        _fail()
    first_pending = group["rows"][prefix]["message_id"]
    if (_meta(store, "owner_delivery_not_sent:" + first_pending) is None
        and _meta(store, "input_cancel_before_send:" + first_pending) is None):
        _fail()
    for row in group["rows"][prefix:]:
        _unstarted(store, settings, row)
    value = {
        "original_job_id": job["job_id"],
        "original_event_id": job["event_id"],
        "original_authority": json.loads(job["authority_json"]),
        "authority": current.authority.as_dict(),
        "privacy_epoch": epoch(store),
        "reply_revision": store.one(
            "SELECT reply_revision FROM conversations WHERE conversation_id=?",
            (job["conversation_id"],)
        )["reply_revision"],
        "prefix": prefix,
        "group_sha256": group["rows"][0]["reply_group_sha256"],
        "parts": [
            {
                k: row[k]
                for k in (
                    "message_id",
                    "content_sha256",
                    "state",
                    "attempt_count",
                    "provider_message_id",
                    "native_ack_json",
                    "sent_at",
                )
            }
            for row in group["rows"]
        ],
    }
    return value, group, current


def preview(store, settings, clock, message_id):
    with store.transaction():
        value, group, _ = _selection(store, settings, clock, message_id)
        existing = _meta(store, ORIGINAL + value["original_job_id"])
        return {
            "state": "CREATED" if existing else "READY",
            "expected_sha256": digest(value),
            "skip_message_ids": [p["message_id"] for p in value["parts"][: value["prefix"]]],
            "source_message_ids": [p["message_id"] for p in value["parts"][value["prefix"] :]],
            "texts": [
                json.loads(p["content_json"])["text"] for p in group["rows"][value["prefix"] :]
            ],
            "recovery": existing,
        }


def create(store, settings, clock, message_id, expected, operation, fingerprint):
    from companion.transport import enqueue_reply_parts

    with nullcontext() if store.db.in_transaction else store.transaction():
        source = store.one("SELECT reply_group_id FROM outbox WHERE message_id=?", (message_id,))
        key = ORIGINAL + source["reply_group_id"] if source and source["reply_group_id"] else ""
        prior = _meta(store, key)
        if prior:
            record = _meta(store, RECORD + prior["job_id"])
            if record["operation_id"] == operation and record["request_sha256"] == fingerprint:
                return {"state": "QUEUED"}
            raise CoreError("REPLY_RECOVERY_ALREADY_CREATED", 409)
        value, group, current = _selection(store, settings, clock, message_id)
        if digest(value) != expected:
            raise CoreError("REPLY_RECOVERY_CHANGED", 409)
        related = store.rows("SELECT job_id FROM jobs j WHERE event_id=? OR EXISTS "
            "(SELECT 1 FROM job_sources s WHERE s.job_id=j.job_id AND s.event_id=?) LIMIT ?",
            (value["original_event_id"], value["original_event_id"], MAX_INPUT_DELIVERIES + 1))
        if len(related) >= MAX_INPUT_DELIVERIES:
            _fail()
        ids = [r["job_id"] for r in related]
        deliveries = store.rows("SELECT o.message_id FROM outbox o LEFT JOIN runtime_meta m "
            "ON m.key='owner_delivery:'||o.message_id WHERE o.origin_event_id=? OR "
            "CASE WHEN json_valid(m.value) THEN json_extract(m.value,'$.event_id')=? OR "
            "json_extract(m.value,'$.job_id') IN (" + ",".join("?" for _ in ids) + ") "
            "ELSE 0 END LIMIT ?", (value["original_event_id"], value["original_event_id"],
                                  *ids, MAX_INPUT_DELIVERIES + 1))
        if len(deliveries) + len(group["rows"]) - value["prefix"] > MAX_INPUT_DELIVERIES:
            _fail()
        now, job_id = clock(), uid("job")
        event_id = "evt_" + digest([KIND, value["original_job_id"]])
        body = {
            "text": "",
            "reply_recovery": {
                "original_job_id": value["original_job_id"],
                "selection_sha256": expected,
                "operation_id": operation,
            },
        }
        source = group["rows"][0]
        store.insert(
            "events",
            event_id=event_id,
            schema_version=1,
            source="owner.console",
            source_event_id=key,
            conversation_id=source["conversation_id"],
            actor_id="owner",
            event_type="reply.recovery.requested",
            occurred_at=now,
            ingested_at=now,
            privacy_scope="private",
            body_json=encode(body),
            trace_id=event_id,
            memory_processed=1,
            reply_revision=value["reply_revision"],
        )
        store.insert(
            "jobs",
            job_id=job_id,
            event_id=event_id,
            kind=KIND,
            state="SUCCEEDED",
            idempotency_key=key,
            max_attempts=1,
            context_tracked=1,
            created_at=now,
            updated_at=now,
            authority_json=encode(current.authority.as_dict()),
        )
        dependencies = {
            value["original_event_id"],
            *(
                r["event_id"]
                for r in store.rows(
                    "SELECT event_id FROM job_sources WHERE job_id=?", (value["original_job_id"],)
                )
            ),
        }
        for source_id in dependencies:
            store.insert("event_dependencies", event_id=event_id, source_event_id=source_id)
            store.insert("job_sources", job_id=job_id, event_id=source_id)
        job = {
            "job_id": job_id,
            "event_id": event_id,
            "conversation_id": source["conversation_id"],
            "kind": KIND,
        }
        texts = [json.loads(p["content_json"])["text"] for p in group["rows"][value["prefix"] :]]
        ids = enqueue_reply_parts(store, job, texts, source["segment_interval_ms"], now)
        record = {
            "kind": KIND,
            "version": 1,
            "selection": value,
            "job_id": job_id,
            "event_id": event_id,
            "message_ids": ids,
            "operation_id": operation,
            "request_sha256": fingerprint,
            "at": now,
        }
        record["sha256"] = digest(record)
        store.insert("runtime_meta", key=RECORD + job_id, value=encode(record))
        store.insert("runtime_meta", key=key, value=encode({"job_id": job_id, "message_ids": ids}))
        for index, new_id in enumerate(ids):
            row = store.one("SELECT * FROM outbox WHERE message_id=?", (new_id,))
            material = _material(store, row, record, index)
            transport.bind_authority(store, new_id, current.authority.as_dict())
            store.insert(
                "runtime_meta", key=owner_delivery.DELIVERY + new_id, value=encode(material)
            )
        store.audit(
            "delivery.recovery_created",
            job_id,
            "console",
            now,
            original_job_id=value["original_job_id"],
            record_sha256=record["sha256"],
            skipped=value["prefix"],
            copied=len(ids),
        )
        return {"state": "QUEUED"}


def _material(store, row, record, index):
    from companion.private_life import event_digest

    event = store.one("SELECT * FROM events WHERE event_id=?", (record["event_id"],))
    return {
        "kind": KIND,
        "authority": record["selection"]["authority"],
        "job_id": record["job_id"],
        "event_id": record["event_id"],
        "event_sha256": event_digest(event),
        "message_id": row["message_id"],
        "action_type": "qq.private.reply",
        "content_sha256": row["content_sha256"],
        "delivery_key": row["delivery_key"],
        "recovery_sha256": record["sha256"],
        "original_message_id": record["selection"]["parts"][record["selection"]["prefix"] + index][
            "message_id"
        ],
    }


def check(store, settings, clock, row, material, body=None):
    """Validate current execution and original evidence independently at every RPC."""
    try:
        record = _meta(store, RECORD + material["job_id"])
        if (
            not record
            or digest({k: v for k, v in record.items() if k != "sha256"}) != record["sha256"]
        ):
            _fail()
        selected = record["selection"]
        value, original, current = _selection(
            store, settings, clock, selected["parts"][0]["message_id"]
        )
        if value != selected:
            _fail()
        job = owner_delivery._job(store, record["job_id"])
        reply_revision.check_sources(store, job["job_id"])
        if (
            job["kind"] != KIND
            or job["state"] != "SUCCEEDED"
            or job["event_id"] != row["origin_event_id"]
            or json.loads(job["authority_json"]) != selected["authority"]
            or json.loads(row["authority_json"]) != selected["authority"]
            or not reply_revision.source_active(store, job["event_id"])
        ):
            _fail()
        index = record["message_ids"].index(row["message_id"])
        source = original["rows"][selected["prefix"] + index]
        if (
            row["content_json"] != source["content_json"]
            or row["content_sha256"] != source["content_sha256"]
            or material != _material(store, row, record, index)
            or body is not None
            and body.reply_revision != selected["reply_revision"]
        ):
            _fail()
        return current
    except (CoreError, KeyError, TypeError, ValueError, InvalidSignature) as error:
        raise CoreError("OWNER_DELIVERY_SOURCE_CHANGED", 403) from error


def history_group(store, message_id, *, include_partial=False):
    """Join the original ACK prefix and the new ACKs only for conversation history.

    Neither physical group nor its delivery identities are rewritten. Each part
    retains the envelope of the actual group that supplied its platform ACK.
    """
    from companion.transport import reply_group, segment_envelopes

    row = store.one("SELECT reply_group_id FROM outbox WHERE message_id=?", (message_id,))
    ref = _meta(store, ORIGINAL + row["reply_group_id"]) if row and row["reply_group_id"] else None
    if ref is None:
        return None
    record = _meta(store, RECORD + ref["job_id"])
    if (not record or record["message_ids"] != ref["message_ids"]
        or digest({k: v for k, v in record.items() if k != "sha256"}) != record["sha256"]):
        _fail()
    old = reply_group(store, message_id)
    new = reply_group(store, record["message_ids"][0], require_sent=not include_partial)
    if new is None:
        return None
    selected = record["selection"]
    prefix = selected["prefix"]
    if (len(new["rows"]) != len(old["rows"]) - prefix
        or old["rows"][0]["reply_group_sha256"] != selected["group_sha256"]
        or any(r["state"] != "SENT" or not r["sent_at"] for r in old["rows"][:prefix])
        or any(r["content_json"] != s["content_json"] for r, s in zip(
            new["rows"], old["rows"][prefix:], strict=True))):
        _fail()
    rows = old["rows"][:prefix] + new["rows"]
    return {"rows": rows, "text": old["text"], "segmented": True,
            "envelopes": segment_envelopes(old) | segment_envelopes(new)}
