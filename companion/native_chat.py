"""Signed native text admission over original owner authority, jobs and evidence."""

import json

from pydantic import Field

from companion import inbox, native_devices, native_images, owner_authority, owner_input, qq_rich
from companion.contracts import CoreError, InboundEvent, MomentRef, StrictModel
from companion.owner_authority_protocol import (
    OwnerCapture,
)
from companion.store import encode, uid
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
    text: str = Field(max_length=20000)
    moment: MomentRef | None = None
    images: list[native_images.NativeImage] = Field(default_factory=list,
                                                    max_length=native_images.MAX_IMAGES)


def device_view(context, public_key=None, *, registered=True):
    device_id = context.session.get("native_device_id")
    if device_id is None:
        raise CoreError("NATIVE_DEVICE_SESSION_REQUIRED", 403)
    if public_key is not None and device_identity(public_key)[0] != device_id:
        raise CoreError("NATIVE_DEVICE_SESSION_MISMATCH", 403)
    view = owner_authority.current(context.store, context.settings, context.clock, purpose="chat")
    if registered:
        native_devices.current_identity(context.store, device_id, view)
    return device_id, view


def device_preview(context, body):
    device_view(context, body.public_key, registered=False)
    return native_devices.preview(context, body.public_key)


def authorize_device(context, body):
    device_view(context, body.public_key, registered=False)
    return native_devices.authorize(context, body)


def recover_authorization(context, body):
    device_id, view = device_view(context, body.public_key, registered=False)
    if native_devices.active(context.store, device_id, view):
        return native_devices.preview(context, body.public_key)
    return None


def submit(context, body, *, content_options):
    device_id, view = device_view(context)
    if body.moment and "moments" not in view.policy.purposes:
        raise CoreError("OWNER_AUTHORITY_PURPOSE_REQUIRED", 403)
    try:
        original_device = source_identity(body.capture.source_event_id)[0]
    except ValueError as error:
        raise CoreError("NATIVE_SOURCE_IDENTITY_REQUIRED", 403) from error
    if body.capture.kind != KIND or original_device != device_id:
        raise CoreError("NATIVE_DEVICE_SESSION_MISMATCH", 403)
    images = native_images.decode(context.settings, body.images) if body.images else []
    rich = native_images.image_rich(body.text, [sha for _, _, sha in images])
    moment = body.moment.model_dump() if body.moment else None
    if ((not body.text.strip() and not rich) or body.capture.input_sha256
            != qq_rich.input_digest({"text": body.text, **({"rich": rich} if rich else {}),
                                     **({"moment": moment} if moment else {})})):
        raise CoreError("NATIVE_INPUT_CHANGED", 403)
    if body.moment and body.moment.kind == "comment":
        from companion import moments

        moments.require_target(context.store, body.moment.moment_id)
    owner_input.approve(context.store, context.settings, context.clock, body.capture)
    event = InboundEvent(schema_version=1, event_id=body.capture.event_id, source=SOURCE,
        source_event_id=body.capture.source_event_id, event_type="message.received",
        occurred_at=body.capture.occurred_at,
        conversation={"platform_session_key": SESSION, "scope": "private"},
        actor={"platform_user_id": "owner", "role": "owner"},
        payload={"text": body.text, "media": [],
                 "shared_content": [{"kind": "image", "slot": slot} for slot in range(len(images))],
                 **({"rich": rich} if rich else {}),
                 **({"moment": moment} if moment else {})}, trace_id=uid("trace"))
    result = inbox.receive(context.store, context.settings, context.clock, context.ledger,
                           event, "native:" + event.event_id, content_options=content_options)
    if images:
        _stage_images(context, event.event_id, images)
    return result


def _stage_images(context, event_id, images):
    """Originals into the owner image intake; a retried submit finds them already staged."""
    from companion import agent_images

    store = context.store
    with store.transaction():
        for slot, (mime, data, _) in enumerate(images):
            row = store.one("SELECT state FROM content_items WHERE owner_event_id = %s AND "
                            "kind = 'qq_image' AND slot = %s", (event_id, slot))
            # Content intake may be off or over its daily cap; the message still stands.
            if row is not None and row["state"] == "WAITING":
                agent_images.accept_native(store, context.settings, context.clock, event_id,
                                           slot, data, mime)
    for slot, (_, data, _) in enumerate(images):
        native_images.save_preview(store, event_id, slot, data)


def image(context, event_id, slot):
    """One attached image's history preview, only while its native message stands."""
    device_view(context)
    row = context.store.one(_HISTORY + "AND e.event_id=%s", (event_id,)) or context.store.one(
        # Moment posts and comments are kept out of the chat history but keep their images.
        "SELECT e.* FROM events e JOIN jobs j USING(event_id) WHERE e.event_id=%s "
        "AND e.source='native.chat' AND e.actor_id='owner' AND j.kind='chat' "
        "AND e.privacy_scope='private' AND e.body_json::jsonb ? 'moment'", (event_id,))
    if (row is None or row["redacted"] or row["actor_id"] != "owner"
            or not any(item.get("kind") == "image" and item.get("slot") == slot
                       for item in json.loads(row["body_json"]).get("shared_content", []))):
        raise CoreError("NATIVE_IMAGE_NOT_FOUND", 404)
    return native_images.read_preview(context.store, event_id, slot)


def _turn(context, row):
    from companion.native_stream import project

    erased = bool(row["redacted"])
    replies = context.store.rows(
        "SELECT message_id,content_json,content_sha256,state,created_at FROM outbox "
        "WHERE origin_event_id=%s "
        "AND action_type IN ('native.private.reply','native.private.proactive') "
        "ORDER BY record_sequence", (row["event_id"],),
    )
    proactive = row["actor_id"] == "system"
    return {"event_id": row["event_id"], "sequence": row["record_sequence"],
            **({"kind": "proactive"} if proactive else {}),
            "source_event_id": row["source_event_id"],
            "text": "" if erased or proactive else json.loads(row["body_json"]).get("text", ""),
            "images": [] if erased or proactive else [
                {"slot": item["slot"]} for item in json.loads(row["body_json"]).get(
                    "shared_content", []) if item.get("kind") == "image"],
            "redacted": erased, "created_at": row["occurred_at"], "state": row["state"],
            "uncertain": bool(row["uncertain"]),
            "error": row["last_error_code"],
            "preview": None if proactive else project(context, row["event_id"], redacted=erased),
            "replies": [_reply(item, erased) for item in replies]}


def _reply(item, erased):
    visible = not erased and item["state"] == "SENT"
    content = json.loads(item["content_json"]) if visible else {}
    value = {"message_id": item["message_id"], "state": item["state"],
             "text": content.get("text", ""), "created_at": item["created_at"]}
    if visible and item['content_json'] == '{}' and not item['content_sha256']:
        value['content_erased'] = True
    # Only a reference: the client shows the quoted text from its own synchronized copy.
    if visible and content.get("quote"):
        value["quote"] = content["quote"]
    # A meme's bytes are read per request, so an erased or excluded asset disappears.
    if visible and content.get("image"):
        value["sticker"] = True
    return value


def sticker(context, message_id):
    """The meme a delivered native reply carries, re-resolved from its frozen offer."""
    from companion import owner_delivery
    from companion.meme_core import MemeCaptionService
    from companion.meme_retrieval import AssetBinding, MemeRetrieval, core_owned

    device_view(context)
    store = context.store
    row = store.one(
        "SELECT o.message_id, o.origin_event_id, o.action_type, o.state, o.content_json "
        "FROM outbox o JOIN events e ON e.event_id = o.origin_event_id JOIN conversations c "
        "ON c.conversation_id = o.conversation_id WHERE o.message_id = %s AND "
        "o.action_type = 'native.private.reply' AND o.state = 'SENT' AND e.redacted = 0 AND "
        "c.platform = 'native.chat' AND c.platform_session_key = %s AND c.scope = 'private' "
        "AND c.owner_actor_id = 'owner'", (message_id, SESSION))
    image = json.loads(row["content_json"]).get("image") if row else None
    saved = store.one("SELECT value FROM runtime_meta WHERE key = %s",
                      (owner_delivery.DELIVERY + message_id,)) if image else None
    if not image or not saved or not core_owned(context.settings):
        raise CoreError("NATIVE_IMAGE_NOT_FOUND", 404)
    job = store.one("SELECT * FROM jobs WHERE job_id = %s",
                    (json.loads(saved["value"])["job_id"],))
    retrieval = MemeRetrieval(MemeCaptionService(store, context.settings, None, context.clock))
    try:
        binding = AssetBinding.model_validate(retrieval.selected(job, image))
        _, _, _, _, original = retrieval._resolve(binding.asset_id, binding.caption_job_id)
    except (CoreError, ValueError):
        raise CoreError("NATIVE_IMAGE_NOT_FOUND", 404) from None
    return original.data, original.mime_type


_HISTORY = (
    "SELECT e.*,j.state,j.last_error_code,ac.preview_version AS stream_version,"
    "ac.preview_json IS NOT NULL AS stream_active,(SELECT jsonb_agg("
    "jsonb_build_array(o.message_id,o.state,o.content_sha256) ORDER BY o.record_sequence) "
    "FROM outbox o WHERE o.origin_event_id=e.event_id) AS stream_replies,"
    "EXISTS(SELECT 1 FROM model_usage mu "
    "WHERE mu.job_id=j.job_id AND mu.state='UNKNOWN') AS uncertain "
    "FROM events e JOIN jobs j USING(event_id) "
    "LEFT JOIN agent_conversations ac USING(job_id) "
    "JOIN conversations c USING(conversation_id) WHERE ((e.source='native.chat' "
    "AND e.actor_id='owner' AND j.kind='chat' "
    # Moment posts and comments live under their moment, not in the conversation.
    "AND NOT (e.body_json::jsonb ? 'moment')) "
    # A proactive message is its own item once delivered; its activity has no owner text.
    "OR (e.source='system.timer' AND e.actor_id='system' AND j.kind='activity' AND EXISTS("
    "SELECT 1 FROM outbox p WHERE p.origin_event_id=e.event_id AND p.action_type="
    "'native.private.proactive' AND p.state='SENT'))) AND e.privacy_scope='private' "
    "AND c.scope='private' AND c.owner_actor_id='owner' AND c.platform='native.chat' "
    "AND c.platform_session_key='native:owner' "
)


def changes(context, previous):
    """Compare public-data revisions, never cache permission or source-check results.

    Inactive history is projected only on changes. Every active preview still checks
    its current proof and lease on every subscription read, including lease expiry.
    """
    from companion.privacy import epoch

    _, view = device_view(context)
    scope = (epoch(context.store), view.authority.revision)
    rows = context.store.rows(_HISTORY + 'ORDER BY e.record_sequence DESC LIMIT 100')
    items, signatures = [], {}
    for row in reversed(rows):
        signature = encode([scope, *[row[name] for name in (
            'state', 'redacted', 'last_error_code', 'uncertain', 'stream_version',
            'stream_replies')]])
        signatures[row['event_id']] = signature
        if row['stream_active'] or previous.get(row['event_id']) != signature:
            items.append(_turn(context, row))
    return items, signatures


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
