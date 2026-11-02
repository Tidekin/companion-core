"""Signed owner input evidence; durable policy and current execution are separate checks."""

import hashlib
import json
import re
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from companion import cumulative, input_recall, owner_authority, paid_budget, reply_revision
from companion.contracts import CoreError
from companion.owner_authority_protocol import OwnerCapture
from companion.privacy import epoch
from companion.qq_rich import input_digest
from companion.store import encode
from companion_delivery_protocol.authority import capture_signing_payload

INPUT = "owner_input:"


def verify(store, settings, value):
    """Verify provenance with the original revision, including after STOP or restore."""
    return _verify(store, settings, value,
                   lambda ref: owner_authority.evidence(store, settings, ref))


def _verify(store, settings, value, evidence):
    try:
        capture = OwnerCapture.model_validate(value)
        view = evidence(capture.authority)
        identity = view.policy.capture
        if identity is None:
            raise ValueError("capture identity absent")
        signed = capture.model_dump(mode="json", exclude={"signature"})
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(identity.public_key)).verify(
            bytes.fromhex(capture.signature), capture_signing_payload(signed))
        prefix = (f"{settings.bot_qq_id}:onebot-direct:FriendMessage:{view.policy.owner_qq_id}:"
                  f"epoch:{identity.protocol_epoch}:")
        row = store.one("SELECT created_at FROM owner_authority_revisions "
                        "WHERE grant_id=? AND revision=?",
                        (view.authority.grant_id, view.authority.revision))
        if (
            capture.authority != view.authority.as_dict()
            or capture.identity_sha256 != identity.identity_sha256
            or capture.protocol_epoch != identity.protocol_epoch
            or not capture.source_event_id.startswith(prefix)
            or not re.fullmatch(r"-?[1-9][0-9]{0,19}", capture.source_event_id[len(prefix):])
            or capture.event_id != "evt_" + hashlib.sha256(
                capture.source_event_id.encode()).hexdigest()
            or not datetime.fromisoformat(row["created_at"]) <= capture.occurred_at
            or not 0 <= (capture.received_at - capture.occurred_at).total_seconds() <= 30
            or (view.policy.expires_at is not None
                and capture.received_at >= view.policy.expires_at)
        ):
            raise ValueError("capture binding changed")
    except (ValueError, KeyError, TypeError, InvalidSignature):
        raise CoreError("OWNER_CAPTURE_INVALID", 403) from None
    return capture, view


def approve(store, settings, clock, capture, *, meme_offer=None):
    """Admit one authenticated frame through the original cumulative input accounting."""
    with store.transaction():
        view = owner_authority.current(store, settings, clock, purpose="chat",
                                       authority=capture.authority)
        value = capture.model_dump(mode="json")
        verify(store, settings, value)
        offer = None
        if meme_offer is not None:
            from companion.expression import MemeOffer

            offer = MemeOffer.model_validate(meme_offer).model_dump(mode="json")
            if (getattr(settings, "meme_catalog_owner", "bridge") == "core"
                or not settings.expression_enabled or settings.meme_semantics_enabled
                or offer["offer_key"] != "meme:" + capture.event_id):
                raise CoreError("OWNER_MEME_OFFER_INVALID", 403)
            for purpose in ("meme", "expression"):
                owner_authority.current(store, settings, clock, purpose=purpose,
                                         authority=capture.authority)
        now = datetime.fromisoformat(clock())
        if (capture.received_at - now).total_seconds() > 2:
            raise CoreError("OWNER_CAPTURE_FUTURE", 403)
        if (now - capture.received_at).total_seconds() >= (
            86400 * min(view.policy.source_retention_days, settings.raw_event_retention_days)
        ):
            raise CoreError("OWNER_INPUT_EXPIRED", 403)
        input_recall.require_not_recalled(store, capture.source_event_id)
        if store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                     ("erased_event:" + capture.event_id,)):
            raise CoreError("EVENT_ERASED", 410)
        key = INPUT + capture.event_id
        prior = store.one("SELECT value FROM runtime_meta WHERE key=?", (key,))
        if prior:
            recorded = json.loads(prior["value"])
            if recorded.get("capture") != value or recorded.get("meme_offer") != offer:
                raise CoreError("OWNER_INPUT_CONFLICT", 409)
            return {"approved": True, "duplicate": True}
        if store.one("SELECT 1 FROM events WHERE event_id=?", (capture.event_id,)):
            raise CoreError("OWNER_HISTORICAL_INPUT_REFUSED", 409)
        cumulative.admit_operation(store, settings, clock(), "input", capture.event_id,
                                    owner_authority._sha(value), at=capture.received_at)
        store.insert("runtime_meta", key=key, value=encode({
            "authority": view.authority.as_dict(), "event_id": capture.event_id,
            "input_sha256": capture.input_sha256, "privacy_epoch": epoch(store),
            "capture": value,
            **({"meme_offer": offer} if offer is not None else {}),
        }))
        return {"approved": True, "duplicate": False}


def check_input(store, settings, clock, event):
    """Bind an inbound event to its already admitted, signed capture before storing it."""
    view = owner_authority.current(store, settings, clock, purpose="chat")
    receipt = store.one("SELECT value FROM runtime_meta WHERE key=?", (INPUT + event.event_id,))
    try:
        value = json.loads(receipt["value"]) if receipt else {}
        capture, _ = verify(store, settings, value["capture"])
        body = event.payload.model_dump(mode="json")
        if (
            capture.authority != view.authority.as_dict()
            or value.get("authority") != capture.authority
            or value.get("event_id") != event.event_id
            or capture.event_id != event.event_id
            or capture.source_event_id != event.source_event_id
            or capture.occurred_at != event.occurred_at
            or capture.input_sha256 != input_digest(body)
            or value.get("input_sha256") != capture.input_sha256
            or type(value.get("privacy_epoch")) is not int
            or not 0 <= value["privacy_epoch"] <= epoch(store)
            or event.source != "qq.onebot" or event.event_type != "message.received"
            or event.actor.platform_user_id != view.policy.owner_qq_id
            or event.conversation.platform_session_key != (
                "onebot-direct:FriendMessage:" + view.policy.owner_qq_id)
            or body.get("media")
            or len(body["text"]) > view.policy.max_input_chars
            or not (body["text"].strip() or body.get("rich"))
            or body.get("rich") and settings.rich_input_scope != "references_v1"
            or any(item.kind not in view.policy.purposes for item in event.payload.shared_content)
        ):
            raise ValueError("capture binding changed")
        now = datetime.fromisoformat(clock())
        if (now - capture.received_at).total_seconds() >= (
            86400 * min(view.policy.source_retention_days, settings.raw_event_retention_days)
        ) or (capture.received_at - now).total_seconds() > 2:
            raise ValueError("capture outside source lifetime")
    except (ValueError, KeyError, TypeError):
        raise CoreError("OWNER_APPROVED_INPUT_REQUIRED", 403) from None
    input_recall.require_not_recalled(store, event.source_event_id)
    if store.one("SELECT 1 FROM runtime_meta WHERE key=?", ("erased_event:" + event.event_id,)):
        raise CoreError("EVENT_ERASED", 410)
    return view


def source(store, settings, view, event_id, now, *, allow_commands=False, history=False,
           purpose="chat"):
    """Read an admitted private source; history requires an explicit current source scope."""
    view = owner_authority.current(store, settings, lambda: now,
        purpose=purpose, authority=view.authority)
    if history:
        view = owner_authority.current(store, settings, lambda: now,
            purpose="history", authority=view.authority)
    return _source(store, settings, view, event_id, now, allow_commands=allow_commands,
                   history=history, verify_capture=lambda value: verify(store, settings, value))


def _source(store, settings, view, event_id, now, *, allow_commands=False, history=False,
            verify_capture):
    event = store.one("SELECT e.*,c.scope,c.owner_actor_id FROM events e "
                      "JOIN conversations c USING(conversation_id) WHERE event_id=?", (event_id,))
    receipt = store.one("SELECT value FROM runtime_meta WHERE key=?", (INPUT + event_id,))
    if history and receipt is None and store.one(
        "SELECT 1 FROM runtime_meta WHERE key=?", ("live_text_input:" + event_id,)
    ):
        from companion import legacy_owner_input

        return legacy_owner_input.source(store, settings, view, event_id, now,
                                          allow_commands=allow_commands)
    try:
        value = json.loads(receipt["value"]) if receipt else {}
        body = json.loads(event["body_json"]) if event else {}
        if (not event or event["redacted"] or not isinstance(body, dict)
            or (event["source"], event["actor_id"], event["scope"], event["owner_actor_id"])
            != ("qq.onebot", "owner", "private", "owner")
            or event["privacy_scope"] != "private"
            or event["event_type"] != "message.received" or body.get("media")
            or not isinstance(body.get("text"), str)
            or not allow_commands and (body.get("control_command") or body.get("diary_command")
                                       or body["text"].strip().startswith("/"))
            or value.get("event_id") != event_id
            or value.get("input_sha256") != input_digest(body)
            or type(value.get("privacy_epoch")) is not int
            or not 0 <= value["privacy_epoch"] <= epoch(store)):
            raise ValueError("source changed")
        capture, original = verify_capture(value["capture"])
        if (value.get("authority") != original.authority.as_dict()
            or capture.event_id != event_id or capture.input_sha256 != value["input_sha256"]
            or capture.source_event_id != event["source_event_id"]
            or capture.occurred_at != datetime.fromisoformat(event["occurred_at"])):
            raise ValueError("capture changed")
        if history:
            scope = view.policy.history
            if ("history" not in view.policy.purposes or scope.scope != "owner_private"
                or capture.occurred_at < scope.not_before):
                raise ValueError("history scope required")
        elif original.authority != view.authority:
            raise ValueError("current source revision changed")
        lifetime = 86400 * min(original.policy.source_retention_days,
                               view.policy.source_retention_days, settings.raw_event_retention_days)
        age = (datetime.fromisoformat(now) - capture.received_at).total_seconds()
        if age < (0 if history else -2) or age >= lifetime:
            raise ValueError("source expired")
    except (ValueError, KeyError, TypeError):
        raise CoreError("OWNER_SOURCE_CHANGED", 403) from None
    input_recall.require_not_recalled(store, event["source_event_id"])
    jobs = store.rows("SELECT * FROM jobs WHERE event_id=? AND kind='chat'", (event_id,))
    if len(jobs) != 1 or json.loads(jobs[0]["authority_json"]) != original.authority.as_dict():
        raise CoreError("OWNER_SOURCE_JOB_BINDING_REQUIRED", 403)
    return event, body, jobs[0]


def history_sources(store, settings, clock, job, *, purpose="chat", cutoff_rowid=None):
    """Apply explicit current history bounds to native source-bound dialogue turns."""
    if not store.db.in_transaction:
        # Own one synchronous batch on one DB state; never retain custody across the
        # model await that separates these calls.
        return store.read_or_write(lambda: _history_sources(
            store, settings, clock, job, purpose=purpose, cutoff_rowid=cutoff_rowid))
    return _history_sources(store, settings, clock, job,
                            purpose=purpose, cutoff_rowid=cutoff_rowid)


def history_batch(store, settings, clock, job, *, purpose):
    """One job authority view for many history sources read from one DB state.

    Inside a write transaction or a read snapshot no other writer's commit is visible,
    so per-source checks reuse the view and capture evidence while this connection has
    not written. Outside them, or after any own write, each source takes the full check.
    `finish` rechecks live authority and independent files for the whole batch.
    """
    view = owner_authority.check_job(store, settings, clock, job, purpose=purpose)

    def frozen():
        return store.in_write_transaction or store.in_read_snapshot

    batched, changes = frozen(), store.db.total_changes
    originals = {encode(view.authority.as_dict()): view}

    def evidence(ref):
        key = encode(ref)
        if key not in originals:
            originals[key] = owner_authority.evidence(store, settings, ref)
        return originals[key]

    def verify_capture(value):
        return _verify(store, settings, value, evidence)

    def check(event_id):
        if batched and frozen() and store.db.total_changes == changes:
            if settings.owner_runtime.mode != "ENABLED":
                raise CoreError("OWNER_AUTHORITY_DISABLED", 403)
            if purpose not in view.policy.purposes or "history" not in view.policy.purposes:
                raise CoreError("OWNER_AUTHORITY_PURPOSE_REQUIRED", 403)
            now = clock()
            if (view.policy.expires_at is not None
                and datetime.fromisoformat(now) >= view.policy.expires_at):
                raise CoreError("OWNER_AUTHORITY_EXPIRED", 403)
            return _source(store, settings, view, event_id, now, history=True,
                           verify_capture=verify_capture)
        return source(store, settings, view, event_id, clock(), purpose=purpose, history=True)

    def finish():
        if batched:
            # Deliberately outside any per-source exclusion handler: STOP, expiry or
            # changed independent custody invalidates the entire batch.
            owner_authority.check_job(store, settings, clock, job, purpose=purpose)
            if not frozen() or store.db.total_changes != changes:
                raise CoreError(reply_revision.CANCELLED, 409)

    return view, check, finish


def _history_sources(store, settings, clock, job, *, purpose, cutoff_rowid):
    from companion.dialogue_history import recent_context_sources, turn_messages

    view, check, finish = history_batch(store, settings, clock, job, purpose=purpose)
    if "history" not in view.policy.purposes:
        return []
    scope = view.policy.history
    result = []
    # The signed scope is an authority ceiling; the existing presentation setting
    # may select fewer recent pairs and leave room for episodic continuation.
    max_turns = min(scope.max_turns, settings.max_recent_messages // 2)
    for item in recent_context_sources(store, job["conversation_id"], job["event_id"],
                                       max_turns, cutoff_rowid=cutoff_rowid):
        try:
            check(item["event_id"])
            parts = item.get("reply_segments") or ([item] if item.get("message_id") else [])
            for part in parts:
                sent = store.one("SELECT sent_at FROM outbox WHERE message_id=?",
                                  (part["message_id"],))
                if (not sent or not sent["sent_at"] or datetime.fromisoformat(sent["sent_at"])
                    > datetime.fromisoformat(clock())):
                    raise CoreError("OWNER_HISTORY_FUTURE_DELIVERY", 403)
            if not store.one("SELECT 1 FROM runtime_meta WHERE key=?", (INPUT + item["event_id"],)):
                from companion import legacy_owner_input

                legacy_owner_input.validate_turn(store, settings, view, item, clock())
        except CoreError:
            continue
        result.append(item)
    while result and sum(len(message["content"].encode()) for item in result
                         for message in turn_messages(item)) > scope.max_utf8_bytes:
        result.pop(0)
    finish()
    return result


def erase(store, event_ids):
    for event_id in event_ids:
        store.execute("DELETE FROM runtime_meta WHERE key=?", (INPUT + event_id,))


def history_proof(sources):
    """Pin the exact admitted input and delivered prefix used by this context."""
    return [{"event_id": item["event_id"], "source_sha256": paid_budget.sha(item)}
            for item in sources]


def check_history(store, settings, clock, job, proof):
    """Recheck selected history, including its original signed input and sent text."""
    try:
        if (not isinstance(proof, list) or len(proof) > 64
            or any(not isinstance(item, dict) or set(item) != {"event_id", "source_sha256"}
                   or not isinstance(item["event_id"], str)
                   or not isinstance(item["source_sha256"], str)
                   or not re.fullmatch(r"[a-f0-9]{64}", item["source_sha256"])
                   for item in proof)
            or len({item["event_id"] for item in proof}) != len(proof)):
            raise ValueError("invalid history proof")
        if not proof:
            return
        current = {item["event_id"]: item for item in history_sources(
            store, settings, clock, job,
            purpose=owner_authority.model_purpose(store, job["kind"], job["job_id"]))}
        actual = history_proof([current[item["event_id"]] for item in proof])
        if actual != proof:
            raise ValueError("history changed")
    except (ValueError, KeyError, TypeError, CoreError) as error:
        raise CoreError(reply_revision.CANCELLED, 409) from error
