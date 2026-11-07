"""Authenticated input lifecycle receipts at the Bridge/Core HTTP boundary."""

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import AwareDatetime, Field

from companion import cumulative, input_recall, live_text, paid_budget, private_life, reply_revision
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityReference
from companion.store import after, encode

RECALL_DOMAIN = b"companion-core/onebot-owner-recall/v1\x00"


class RecallReceipt(live_text.WindowReference):
    kind: Literal["authenticated_onebot_friend_recall"]
    recall_id: str = Field(pattern=r"^recall_[a-f0-9]{64}$")
    target_source_event_id: str = Field(min_length=1, max_length=512)
    target_event_id: str = Field(pattern=r"^evt_[a-f0-9]{64}$")
    protocol_epoch: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    frame_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    signature: str = Field(pattern=r"^[a-f0-9]{128}$")


class CancelBeforeSend(live_text.WindowReference):
    message_id: str = Field(pattern=r"^msg_[a-f0-9]{32}$")
    lease_token: str = Field(pattern=r"^lease_[a-f0-9]{32}$")
    reply_revision: int | None = Field(default=None, ge=0, strict=True,
                                       exclude_if=lambda value: value is None)
    content_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$",
                                      exclude_if=lambda value: value is None)
    cause: Literal["OWNER_INPUT_PENDING"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    pending_event_id: str | None = Field(default=None, pattern=r"^evt_[a-f0-9]{64}$",
                                        exclude_if=lambda value: value is None)


class OwnerCancelBeforeSend(AuthorityReference):
    message_id: str = Field(pattern=r"^msg_[a-f0-9]{32}$")
    lease_token: str = Field(pattern=r"^lease_[a-f0-9]{32}$")
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reply_revision: int | None = Field(default=None, ge=0, strict=True,
                                       exclude_if=lambda value: value is None)
    cause: Literal["OWNER_INPUT_PENDING"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    pending_event_id: str | None = Field(default=None, pattern=r"^evt_[a-f0-9]{64}$",
                                        exclude_if=lambda value: value is None)


def approval_for_receipt(store, settings, window_sha256=None):
    # Read/revocation receipts remain available after STOP, expiry or an UNKNOWN.
    approval = settings.live_text_window
    if not private_life.enabled(settings) or approval is None:
        raise CoreError("COMPANION_INPUT_LIFECYCLE_REQUIRED", 403)
    if window_sha256 is not None and window_sha256 != approval.plan_sha256:
        raise CoreError("LIVE_TEXT_WINDOW_MISMATCH", 403)
    binding = live_text._meta(store, live_text.BINDING)
    if not binding or binding.get("plan_sha256") != approval.plan_sha256:
        raise CoreError("LIVE_TEXT_WINDOW_MISMATCH", 403)
    return approval


def recall_ledger_extension(store, settings, old_digest, new_digest):
    """Recognize only a complete append of authenticated recall records.

    Notices without an existing source do not advance the source epoch. Their fsynced
    ledger extension still needs explicit custody validation, including recovery
    after the ledger append committed but the receipt transaction did not.
    """
    try:
        data = Path(settings.erasure_ledger).read_bytes()
        if hashlib.sha256(data).hexdigest() != new_digest:
            return False
        prefix, extension = bytearray(), []
        found = False
        for line in data.splitlines(keepends=True):
            if found:
                extension.append(json.loads(line))
            else:
                prefix.extend(line)
                found = hashlib.sha256(prefix).hexdigest() == old_digest
        return bool(found and extension and all(
            record.get("target_type") == "input_recall"
            and record.get("state") in {"PENDING_TARGET", "RECALLED"}
            and store.one("SELECT 1 FROM privacy_erasure_ledger WHERE erasure_id=? "
                          "AND state='COMPLETE'", (record["erasure_id"],))
            for record in extension
        ))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _signature(approval, value, domain):
    try:
        signed = {key: item for key, item in value.items() if key != "signature"}
        for key in ("received_at", "occurred_at"):
            signed[key] = datetime.fromisoformat(value[key]).isoformat()
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(approval.capture_public_key or "")).verify(
            bytes.fromhex(value["signature"]), domain + encode(signed).encode(),
        )
    except (ValueError, KeyError, TypeError, InvalidSignature):
        raise CoreError("INPUT_LIFECYCLE_SIGNATURE_REQUIRED", 403) from None


def _bound_epoch(store, approval, protocol_epoch):
    epochs = {row["epoch"] for row in store.rows(
        "SELECT DISTINCT json_extract(value,'$.capture.protocol_epoch') AS epoch "
        "FROM runtime_meta WHERE key LIKE 'live_text_input:%' "
        "AND json_extract(value,'$.window_sha256')=?", (approval.plan_sha256,),
    ) if row["epoch"] is not None}
    if approval.handoff:
        epochs.add(approval.handoff.protocol_epoch)
    key = "input_protocol_epoch:" + approval.plan_sha256
    saved = store.one("SELECT value FROM runtime_meta WHERE key=?", (key,))
    if saved:
        epochs.add(saved["value"])
    if epochs and epochs != {protocol_epoch}:
        raise CoreError("INPUT_RECALL_PROTOCOL_EPOCH_CHANGED", 403)
    store.execute("INSERT OR IGNORE INTO runtime_meta VALUES (?,?)", (key, protocol_epoch))


def _source_approval(store, settings, approval, source_plan, source):
    """Follow only the already accepted owner/epoch handoff chain."""
    current, seen = approval, set()
    while current.plan_sha256 not in seen and len(seen) < 128:
        if current.plan_sha256 == source_plan:
            return current
        seen.add(current.plan_sha256)
        handoff = current.handoff
        if (not handoff or handoff.protocol_epoch != source["protocol_epoch"]
            or handoff.identity_sha256 != paid_budget.sha(
                [settings.owner_qq_id, settings.bot_qq_id])):
            return None
        previous = live_text._meta(store, live_text.HISTORY + handoff.predecessor_plan_sha256)
        if (not previous or previous.get("state") != "RETIRED"
            or previous.get("successor_plan_sha256") != current.plan_sha256):
            return None
        try:
            current = type(approval).model_validate(previous["approval"])
        except (KeyError, ValueError, TypeError):
            return None
        if (current.plan_sha256 != handoff.predecessor_plan_sha256
            or current.window_id != handoff.predecessor_window_id):
            return None
    return None


def _capture_matches(store, settings, approval, source, event_id, occurred_at):
    receipt = live_text._meta(store, live_text.INPUT + event_id)
    capture = receipt.get("capture") if receipt else None
    if not capture:
        return False
    original = _source_approval(store, settings, approval, receipt.get("window_sha256"), source)
    if original is None:
        return False
    _signature(original, capture, live_text.CAPTURE_SIGNATURE_DOMAIN)
    original_time = datetime.fromisoformat(capture["occurred_at"])
    received = datetime.fromisoformat(capture["received_at"])
    if (
        receipt.get("window_sha256") != original.plan_sha256
        or receipt.get("event_id") != event_id
        or capture.get("kind") != "authenticated_onebot_ws"
        or capture.get("window_sha256") != original.plan_sha256
        or capture.get("source_event_id") != source["source_event_id"]
        or capture.get("event_id") != event_id
        or capture.get("input_sha256") != receipt.get("input_sha256")
        or capture.get("identity_sha256") != paid_budget.sha(
            [settings.owner_qq_id, settings.bot_qq_id])
        or capture.get("protocol_epoch") != source["protocol_epoch"]
        or not original.starts_at <= original_time <= received < original.ends_at
        or not 0 <= (received - original_time).total_seconds() <= 30
        or original_time > datetime.fromisoformat(occurred_at)
    ):
        return False
    # Standard friend_recall omits direction. A captured inbound is necessary;
    # conflicting outgoing evidence must never be guessed into an owner recall.
    return not store.one(
        "SELECT 1 FROM outbox o JOIN conversations c USING(conversation_id) "
        "WHERE o.provider_message_id=? AND c.platform_session_key=? LIMIT 1",
        (source["provider_message_id"], source["platform_session_key"]),
    )


def receive_recall(store, settings, ledger, clock, body):
    with store.transaction():
        approval = approval_for_receipt(store, settings, body.window_sha256)
        value = body.model_dump(mode="json")
        _signature(approval, value, RECALL_DOMAIN)
        try:
            source = input_recall.canonical_source(
                bot_qq_id=settings.bot_qq_id, owner_qq_id=settings.owner_qq_id,
                protocol_epoch=body.protocol_epoch,
                provider_message_id=body.target_source_event_id.rsplit(":", 1)[-1],
            )
        except ValueError:
            raise CoreError("INPUT_RECALL_BINDING_CHANGED", 403) from None
        occurred = body.occurred_at.astimezone(UTC).isoformat()
        if (
            body.target_source_event_id != source["source_event_id"]
            or body.target_event_id != "evt_" + hashlib.sha256(
                source["source_event_id"].encode()).hexdigest()
            or body.recall_id != "recall_" + hashlib.sha256(
                (source["source_event_id"] + occurred).encode()).hexdigest()
            or body.identity_sha256 != paid_budget.sha([settings.owner_qq_id, settings.bot_qq_id])
            or not approval.starts_at <= body.occurred_at <= body.received_at < approval.ends_at
            or (body.received_at - datetime.fromisoformat(clock())).total_seconds() > 2
        ):
            raise CoreError("INPUT_RECALL_BINDING_CHANGED", 403)
        _bound_epoch(store, approval, body.protocol_epoch)
        matched = _capture_matches(
            store, settings, approval, source, body.target_event_id, occurred,
        )
        result = input_recall.recall_input(
            store, ledger, source, clock(), verified=matched, occurred_at=occurred,
            event_id=body.target_event_id if matched else None,
            retention_cutoff=after(clock(), -86400 * settings.raw_event_retention_days),
        )
        row = store.one("SELECT reply_revision FROM events WHERE event_id=?",
                        (body.target_event_id,))
        return {"recall_id": body.recall_id, "target_event_id": body.target_event_id,
                "state": result["state"], "duplicate": result["duplicate"],
                "reply_revision": row["reply_revision"] if row else 0}


@contextmanager
def admit_unrecalled(store, settings, ledger, clock, event):
    """Hold the write lock from recall reconciliation through inbox acceptance."""
    with store.transaction():
        _apply_pending_input(store, settings, ledger, clock, event)
        if not input_recall.is_recalled(store, event.source_event_id):
            yield
            return
    # A verified recall must commit before the Spool receives its terminal 410.
    raise CoreError("EVENT_ERASED", 410)


def _apply_pending_input(store, settings, ledger, clock, event):
    """Resolve a pending recall under the inbox admission write lock."""
    from companion import owner_authority, owner_recall

    if owner_authority.required(store, settings):
        return owner_recall.apply_pending(store, settings, ledger, clock, event)
    if not private_life.enabled(settings):
        return
    marker = live_text._meta(store, input_recall.marker_key(event.source_event_id))
    if not marker or marker.get("state") != "PENDING_TARGET":
        return
    approval = approval_for_receipt(store, settings)
    source = input_recall.canonical_source(**{key: marker[key] for key in (
        "bot_qq_id", "owner_qq_id", "protocol_epoch", "provider_message_id",
        "platform_session_key",
    )})
    if (event.source_event_id != source["source_event_id"]
        or event.event_id != "evt_" + hashlib.sha256(event.source_event_id.encode()).hexdigest()
        or event.actor.platform_user_id != settings.owner_qq_id
        or event.conversation.platform_session_key != source["platform_session_key"]
        or not _capture_matches(store, settings, approval, source, event.event_id,
                                marker["occurred_at"])):
        return
    input_recall.recall_input(
        store, ledger, source, clock(), verified=True, occurred_at=marker["occurred_at"],
        event_id=event.event_id,
        retention_cutoff=after(clock(), -86400 * settings.raw_event_retention_days),
    )


def input_state(store, settings, event_id):
    approval = approval_for_receipt(store, settings)
    receipt = live_text._meta(store, live_text.INPUT + event_id)
    if not receipt or receipt.get("window_sha256") != approval.plan_sha256:
        raise CoreError("LIVE_TEXT_APPROVED_INPUT_REQUIRED", 404)
    event = store.one("SELECT * FROM events WHERE event_id=?", (event_id,))
    job = store.one("SELECT * FROM jobs WHERE event_id=? AND kind='chat'", (event_id,))
    deliveries = store.rows("SELECT message_id,state FROM outbox WHERE origin_event_id=? "
                            "AND action_type='qq.private.reply'", (event_id,))
    erased = bool(store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                            ("erased_event:" + event_id,)))
    if erased or (event and event["redacted"]):
        state = "RECALLED"
    elif deliveries and all(item["state"] == "SENT" for item in deliveries):
        state = "ACK"
    elif (event and not reply_revision.current(store, event_id)) or (
        job and job["last_error_code"] in {reply_revision.SUPERSEDED, "REPLY_INTERRUPTED"}
    ) or (
        deliveries and all(item["state"] in {"SENT", "CANCELLED"} for item in deliveries)
        and any(item["state"] == "CANCELLED" and live_text._meta(
            store, "input_cancel_before_send:" + item["message_id"],
        ) for item in deliveries)
    ):
        state = "SUPERSEDED"
    elif job and job["state"] == "CANCELLED":
        state = "RECALLED" if job["last_error_code"] in {
            "INPUT_RECALLED", reply_revision.SOURCE_RECALLED,
        } else "FAILED"
    elif job and job["state"] == "SUCCEEDED":
        state = "REPLIED"
    elif job and job["state"] in {"FAILED", "CANCELLED"}:
        state = "FAILED"
    else:
        state = "RUNNING" if job and job["state"] == "RUNNING" else "QUEUED"
    return {
        "window_sha256": approval.plan_sha256, "event_id": event_id,
        "input_sha256": receipt["input_sha256"],
        "reply_revision": event["reply_revision"] if event else 0, "state": state,
        "unsettled_model": bool(job and store.one(
            "SELECT 1 FROM model_usage WHERE job_id=? AND state IN ('RESERVED','UNKNOWN')",
            (job["job_id"],))),
        "unsettled_delivery": any(item["state"] in {"SENDING", "DELIVERY_UNCERTAIN"}
                                   for item in deliveries),
        "reason": job["last_error_code"] if job else None,
    }


def cancel_before_send(store, settings, clock, body):
    """Consume the Bridge's narrow pre-delegate non-dispatch attestation."""
    with store.transaction():
        native = isinstance(body, OwnerCancelBeforeSend)
        if native:
            from companion import owner_authority

            approval = owner_authority.evidence(store, settings, body.authority)
        else:
            approval = approval_for_receipt(store, settings, body.window_sha256)
        key = "input_cancel_before_send:" + body.message_id
        prior = live_text._meta(store, key)
        material = body.model_dump(mode="json")
        if prior:
            if prior != material:
                raise CoreError("INPUT_CANCEL_RECEIPT_CONFLICT", 409)
        else:
            row = store.one("SELECT o.*,e.reply_revision FROM outbox o JOIN events e "
                            "ON e.event_id=o.origin_event_id WHERE o.message_id=?",
                            (body.message_id,))
            if (not row or row["lease_token"] != body.lease_token
                or row["state"] not in {"LEASED", "SENDING", "DELIVERY_UNCERTAIN", "CANCELLED"}):
                raise CoreError("INPUT_CANCEL_BEFORE_SEND_UNPROVEN", 409)
            pending = (body.cause == "OWNER_INPUT_PENDING" and body.pending_event_id is not None
                       and body.pending_event_id != row["origin_event_id"]
                       and row["state"] in {"LEASED", "SENDING"})
            if (body.cause is None) != (body.pending_event_id is None):
                raise CoreError("INPUT_CANCEL_BEFORE_SEND_UNPROVEN", 409)
            record = live_text._meta(store, ("owner_delivery:" if native else private_life.DELIVERY)
                                     + body.message_id)
            expected = {**({"authority": body.authority} if native else {
                            "plan_sha256": body.window_sha256}),
                        "message_id": body.message_id, "event_id": row["origin_event_id"],
                        "action_type": row["action_type"],
                        "content_sha256": row["content_sha256"],
                        "delivery_key": row["delivery_key"]}
            bound_delivery = record and all(record.get(key) == value
                                            for key, value in expected.items())
            if native and (json.loads(row["authority_json"]) != body.authority
                or not record or record.get("authority") != body.authority
                or body.content_sha256 != record.get("content_sha256")
                or any(record.get(key) != value for key, value in expected.items()
                       if key != "content_sha256")
                or store.one("SELECT 1 FROM audit_log WHERE action='delivery.result' "
                             "AND entity_id=?",
                             (body.message_id,))):
                raise CoreError("INPUT_CANCEL_BEFORE_SEND_UNPROVEN", 409)
            if native and row["content_sha256"] != body.content_sha256:
                erased_source = store.one(
                    "SELECT 1 FROM job_sources s JOIN events e USING(event_id) "
                    "WHERE s.job_id=? AND (e.redacted=1 OR EXISTS "
                    "(SELECT 1 FROM runtime_meta WHERE key='erased_event:'||e.event_id)) LIMIT 1",
                    (record.get("job_id"),))
                if row["content_sha256"] != "" or not erased_source:
                    raise CoreError("INPUT_CANCEL_BEFORE_SEND_UNPROVEN", 409)
            poke_delivery = bool(store.one(
                "SELECT 1 FROM poke_batches WHERE response_message_id=?", (body.message_id,),
            ))
            if poke_delivery:
                erased = bool(store.one(
                    "SELECT 1 FROM poke_batches p JOIN job_sources s USING(job_id) "
                    "JOIN events e USING(event_id) WHERE p.response_message_id=? "
                    "AND (e.redacted=1 OR EXISTS (SELECT 1 FROM runtime_meta "
                    "WHERE key='erased_event:' || e.event_id)) LIMIT 1", (body.message_id,),
                ))
                identity = record and all(record.get(key) == value
                    for key, value in expected.items() if key != "content_sha256")
                if (row["action_type"] not in {"qq.private.reply", "qq.private.poke"}
                    or body.reply_revision is not None or not identity
                    or body.content_sha256 != record.get("content_sha256")
                    or (row["content_sha256"] != body.content_sha256
                        and not (erased and row["content_sha256"] == ""))
                    or (body.cause is not None and not pending)
                    or store.one("SELECT 1 FROM audit_log WHERE action='delivery.result' "
                                 "AND entity_id=?", (body.message_id,))):
                    raise CoreError("INPUT_CANCEL_BEFORE_SEND_UNPROVEN", 409)
            elif row["action_type"] == "qq.private.reply":
                if not reply_revision.owner_input(store, row["origin_event_id"]):
                    # Required notices have no owner-input revision. Their original
                    # Core authorization survives source erasure as a narrow proof
                    # of identity, never as renewed permission to dispatch.
                    erased = bool(store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                                             ("erased_event:" + row["origin_event_id"],)))
                    notice_identity = record and all(record.get(key) == value
                        for key, value in expected.items() if key != "content_sha256")
                    if (body.reply_revision is not None or not notice_identity
                        or (not native and record.get("window_notice") != "commitment")
                        or (native and not store.one("SELECT 1 FROM jobs WHERE job_id=? "
                            "AND kind='activity'", (record.get("job_id"),)))
                        or body.content_sha256 != record.get("content_sha256")
                        or (row["content_sha256"] != body.content_sha256
                            and not (erased and row["content_sha256"] == ""))
                        or body.cause is not None
                        or (row["state"] == "DELIVERY_UNCERTAIN"
                            and (not erased or row["last_error_code"]))
                        or store.one("SELECT 1 FROM audit_log WHERE action='delivery.result' "
                                     "AND entity_id=?", (body.message_id,))):
                        raise CoreError("INPUT_CANCEL_BEFORE_SEND_UNPROVEN", 409)
                elif row["reply_revision"] != body.reply_revision:
                    raise CoreError("INPUT_CANCEL_BEFORE_SEND_UNPROVEN", 409)
            elif row["action_type"] == "qq.private.proactive":
                if not pending or body.reply_revision is not None or not bound_delivery:
                    raise CoreError("INPUT_CANCEL_BEFORE_SEND_UNPROVEN", 409)
            else:
                raise CoreError("INPUT_CANCEL_BEFORE_SEND_UNPROVEN", 409)
            if not pending:
                try:
                    if native:
                        reply_revision.check_delivery(store, body.message_id)
                    if poke_delivery:
                        from companion import poke

                        poke.check_delivery(store, settings, approval, body.message_id, clock=clock)
                    else:
                        reply_revision.check_delivery(store, body.message_id)
                except CoreError as error:
                    if error.code not in {reply_revision.SUPERSEDED, reply_revision.SOURCE_RECALLED,
                                          reply_revision.CANCELLED,
                                          *({"POKE_CONTEXT_SUPERSEDED", "POKE_CONTEXT_CHANGED",
                                             "PRIVATE_POKE_SOURCE_CHANGED"}
                                            if poke_delivery else set())}:
                        raise
                else:
                    raise CoreError("INPUT_CANCEL_BEFORE_SEND_UNPROVEN", 409)
            from companion.transport import cancel_origin_replies, mark_not_dispatched

            # The authenticated Bridge may already have a new signed owner frame
            # queued while this HTTP check is in flight. Its explicit pre-delegate
            # attestation cancels delivery only; inbox admission still owns revision.
            # FAILED closes the quota operation but does not refund its reservation.
            cumulative.send_result(store, body.message_id, "FAILED", clock())
            mark_not_dispatched(store, body.message_id, clock(),
                                attempt_count=max(row["attempt_count"], 1))
            if row["action_type"] == "qq.private.reply":
                # The queued replacement may itself be recalled before ingestion.
                # Once interrupted, no unsent tail of this reply may resume.
                cancel_origin_replies(store, row["origin_event_id"], clock())
            live_text._put(store, key, material)
        return {"cancelled": True, "message_id": body.message_id,
                "state": "CANCELLED", "send_attempt_retained": True}
