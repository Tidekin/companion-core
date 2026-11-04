"""Authenticated v2 withdrawals reuse original capture proof and ErasureLedger."""

import hashlib
import json
from datetime import UTC, datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from companion import input_recall, owner_authority, owner_input
from companion.contracts import CoreError
from companion.store import after
from companion_delivery_protocol.authority import recall_signing_payload


def capture_matches(store, settings, source, event_id, occurred_at):
    saved = store.one("SELECT value FROM runtime_meta WHERE key=?", (owner_input.INPUT + event_id,))
    if not saved:
        return False
    value = json.loads(saved["value"])
    capture, _ = owner_input.verify(store, settings, value["capture"])
    return bool(
        value.get("event_id") == capture.event_id == event_id
        and value.get("authority") == capture.authority
        and value.get("input_sha256") == capture.input_sha256
        and capture.source_event_id == source["source_event_id"]
        and capture.protocol_epoch == source["protocol_epoch"]
        and capture.occurred_at <= datetime.fromisoformat(occurred_at)
        and not store.one(
            "SELECT 1 FROM outbox o JOIN conversations c USING(conversation_id) "
            "WHERE o.provider_message_id=? AND c.platform_session_key=? LIMIT 1",
            (source["provider_message_id"], source["platform_session_key"]))
    )


def receive(store, settings, ledger, clock, body):
    with store.transaction():
        # Withdrawal is evidence processing, including after STOP or restore.
        view = owner_authority.evidence(store, settings, body.authority)
        identity = view.policy.capture
        try:
            if identity is None:
                raise ValueError("capture identity required")
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(identity.public_key)).verify(
                bytes.fromhex(body.signature),
                recall_signing_payload(body.model_dump(mode="json", exclude={"signature"})))
            source = input_recall.canonical_source(
                bot_qq_id=settings.bot_qq_id, owner_qq_id=settings.owner_qq_id,
                protocol_epoch=body.protocol_epoch,
                provider_message_id=body.target_source_event_id.rsplit(":", 1)[-1])
            occurred = body.occurred_at.astimezone(UTC).isoformat()
            created = store.one("SELECT created_at FROM owner_authority_revisions "
                "WHERE grant_id=? AND revision=?", (view.authority.grant_id,
                                                    view.authority.revision))["created_at"]
            if (body.target_source_event_id != source["source_event_id"]
                or body.target_event_id != "evt_" + hashlib.sha256(
                    source["source_event_id"].encode()).hexdigest()
                or body.recall_id != "recall_" + hashlib.sha256(
                    (source["source_event_id"] + occurred).encode()).hexdigest()
                or body.protocol_epoch != identity.protocol_epoch
                or body.identity_sha256 != identity.identity_sha256
                or view.policy.owner_qq_id != settings.owner_qq_id
                or not datetime.fromisoformat(created) <= body.occurred_at <= body.received_at
                or (body.received_at - body.occurred_at).total_seconds() > 30
                or (body.received_at - datetime.fromisoformat(clock())).total_seconds() > 2):
                raise ValueError("recall binding changed")
        except (ValueError, KeyError, TypeError, InvalidSignature):
            raise CoreError("OWNER_RECALL_INVALID", 403) from None
        matched = capture_matches(store, settings, source, body.target_event_id, occurred)
        result = input_recall.recall_input(store, ledger, source, clock(), verified=matched,
            occurred_at=occurred, event_id=body.target_event_id if matched else None,
            retention_cutoff=after(clock(), -86400 * settings.raw_event_retention_days))
        row = store.one("SELECT reply_revision FROM events WHERE event_id=?",
                        (body.target_event_id,))
        return {"recall_id": body.recall_id, "target_event_id": body.target_event_id,
                "state": result["state"], "duplicate": result["duplicate"],
                "reply_revision": row["reply_revision"] if row else 0}


def apply_pending(store, settings, ledger, clock, event):
    saved = store.one("SELECT value FROM runtime_meta WHERE key=?",
                      (input_recall.marker_key(event.source_event_id),))
    marker = json.loads(saved["value"]) if saved else None
    if not marker or marker.get("state") != "PENDING_TARGET":
        return
    source = input_recall.canonical_source(**{key: marker[key] for key in (
        "bot_qq_id", "owner_qq_id", "protocol_epoch", "provider_message_id",
        "platform_session_key")})
    if (event.source_event_id != source["source_event_id"]
        or event.event_id != "evt_" + hashlib.sha256(event.source_event_id.encode()).hexdigest()
        or event.actor.platform_user_id != settings.owner_qq_id
        or event.conversation.platform_session_key != source["platform_session_key"]
        or not capture_matches(store, settings, source, event.event_id, marker["occurred_at"])):
        return
    input_recall.recall_input(store, ledger, source, clock(), verified=True,
        occurred_at=marker["occurred_at"], event_id=event.event_id,
        retention_cutoff=after(clock(), -86400 * settings.raw_event_retention_days))
