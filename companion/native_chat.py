"""Signed native text admission over original owner authority, jobs and evidence."""

import json

from pydantic import Field

from companion import inbox, owner_authority, owner_input, qq_rich
from companion.contracts import CoreError, InboundEvent, StrictModel
from companion.owner_authority_protocol import (
    AuthorityChange,
    AuthorityPreview,
    OwnerCapture,
    OwnerGrantPolicy,
)
from companion.store import uid
from companion_delivery_protocol.native_chat import (
    KIND,
    SESSION,
    SOURCE,
    device_identity,
    source_identity,
)


class Device(StrictModel):
    public_key: str = Field(pattern=r"^[a-f0-9]{64}$")


class DeviceApproval(Device):
    expected_revision: int = Field(ge=0, strict=True)
    preview_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Submit(StrictModel):
    capture: OwnerCapture
    text: str = Field(min_length=1, max_length=20000)


def device_view(context, public_key=None, *, registered=True):
    device_id = context.session.get("native_device_id")
    if device_id is None:
        raise CoreError("NATIVE_DEVICE_SESSION_REQUIRED", 403)
    if public_key is not None and device_identity(public_key)[0] != device_id:
        raise CoreError("NATIVE_DEVICE_SESSION_MISMATCH", 403)
    view = owner_authority.current(context.store, context.settings, context.clock, purpose="chat")
    if registered and device_id not in view.policy.native_devices:
        raise CoreError("NATIVE_DEVICE_APPROVAL_REQUIRED", 403)
    return device_id, view


def _policy(view, public_key):
    device_id, identity = device_identity(public_key)
    if device_id not in view.policy.native_devices and len(view.policy.native_devices) >= 16:
        raise CoreError("NATIVE_DEVICE_LIMIT", 409)
    policy = view.policy.model_dump()
    policy["native_devices"] = {**policy.get("native_devices", {}), device_id: identity}
    return OwnerGrantPolicy.model_validate(policy)


def device_preview(context, body):
    device_id, view = device_view(context, body.public_key, registered=False)
    if device_id in view.policy.native_devices:
        return {"registered": True, "device_id": device_id, "authority": view.authority.as_dict()}
    preview = owner_authority.preview(context.store, context.settings, context.clock,
        AuthorityPreview(expected_revision=view.authority.revision,
                         policy=_policy(view, body.public_key)))
    return {"registered": False, "device_id": device_id,
            "expected_revision": view.authority.revision,
            "preview_sha256": preview["preview_sha256"]}


def authorize_device(context, body):
    device_id, view = device_view(context, body.public_key, registered=False)
    if device_id in view.policy.native_devices:
        return {"registered": True, "device_id": device_id, "authority": view.authority.as_dict()}
    result = owner_authority.activate(context.store, context.settings, context.clock,
        AuthorityChange(operation_id="native_device_" + context.session["token_sha256"][:32],
                        expected_revision=body.expected_revision,
                        policy=_policy(view, body.public_key),
                        preview_sha256=body.preview_sha256))
    return {"registered": True, "device_id": device_id, "authority": result["authority"]}


def recover_authorization(context, body):
    device_id, view = device_view(context, body.public_key, registered=False)
    if device_id in view.policy.native_devices:
        return {"registered": True, "device_id": device_id, "authority": view.authority.as_dict()}
    return None


def submit(context, body, *, content_options):
    device_id, _ = device_view(context)
    try:
        original_device = source_identity(body.capture.source_event_id)[0]
    except ValueError as error:
        raise CoreError("NATIVE_SOURCE_IDENTITY_REQUIRED", 403) from error
    if body.capture.kind != KIND or original_device != device_id:
        raise CoreError("NATIVE_DEVICE_SESSION_MISMATCH", 403)
    if (not body.text.strip() or
            body.capture.input_sha256 != qq_rich.input_digest({"text": body.text})):
        raise CoreError("NATIVE_INPUT_CHANGED", 403)
    owner_input.approve(context.store, context.settings, context.clock, body.capture)
    event = InboundEvent(schema_version=1, event_id=body.capture.event_id, source=SOURCE,
        source_event_id=body.capture.source_event_id, event_type="message.received",
        occurred_at=body.capture.occurred_at,
        conversation={"platform_session_key": SESSION, "scope": "private"},
        actor={"platform_user_id": "owner", "role": "owner"},
        payload={"text": body.text, "media": [], "shared_content": []}, trace_id=uid("trace"))
    return inbox.receive(context.store, context.settings, context.clock, context.ledger,
                         event, "native:" + event.event_id, content_options=content_options)


def _turn(context, row):
    erased = bool(row["redacted"])
    replies = context.store.rows(
        "SELECT message_id,content_json,state,created_at FROM outbox WHERE origin_event_id=%s "
        "AND action_type IN ('native.private.reply','native.private.proactive') "
        "ORDER BY record_sequence", (row["event_id"],),
    )
    return {"event_id": row["event_id"], "sequence": row["record_sequence"],
            "source_event_id": row["source_event_id"],
            "text": "" if erased else json.loads(row["body_json"]).get("text", ""),
            "redacted": erased, "created_at": row["occurred_at"], "state": row["state"],
            "uncertain": bool(row["uncertain"]),
            "error": row["last_error_code"],
            "replies": [{"message_id": item["message_id"], "state": item["state"],
                         "text": ("" if erased or item["state"] != "SENT" else
                                  json.loads(item["content_json"]).get("text", "")),
                         "created_at": item["created_at"]} for item in replies]}


_HISTORY = (
    "SELECT e.*,j.state,j.last_error_code,EXISTS(SELECT 1 FROM model_usage mu "
    "WHERE mu.job_id=j.job_id AND mu.state='UNKNOWN') AS uncertain "
    "FROM events e JOIN jobs j USING(event_id) "
    "JOIN conversations c USING(conversation_id) WHERE e.source='native.chat' "
    "AND e.actor_id='owner' AND e.privacy_scope='private' AND j.kind='chat' "
    "AND c.scope='private' AND c.owner_actor_id='owner' AND c.platform='native.chat' "
    "AND c.platform_session_key='native:owner' "
)


def history(context, *, before=None, limit=50, event_id=None):
    device_view(context)
    if not 1 <= limit <= 100 or before is not None and before < 1:
        raise CoreError("INVALID_PAGE", 400)
    parameters = []
    query = _HISTORY
    if event_id is not None:
        query += "AND e.event_id=%s "
        parameters.append(event_id)
    if before is not None:
        query += "AND e.record_sequence<%s "
        parameters.append(before)
    rows = context.store.rows(query + "ORDER BY e.record_sequence DESC LIMIT %s",
                              (*parameters, limit + 1))
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [_turn(context, row) for row in reversed(rows)],
            "next_before": rows[-1]["record_sequence"] if rows and more else None}
