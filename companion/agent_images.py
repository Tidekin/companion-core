"""Native conversational images retain their signed capture and original expiry.

Original bytes remain in the existing short-lived spool. Stored SDK conversations
use ImageUrl references to those originals; Core rehydrates them before the SDK
run. Neither the model adapter nor the event journal acquires file custody.
"""

import hashlib
import json
import re
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from companion import image_spool, owner_authority, owner_input, qq_images, reply_revision
from companion.contracts import CoreError
from companion.image_frames import prepare
from companion.privacy import epoch
from companion.store import after, encode, uid

REFERENCE = "companion-image://"


def enabled(settings):
    return bool(settings.owner_runtime.mode == "ENABLED" and settings.paid_model
                and settings.paid_model.image_input and settings.content_enabled)


def erase_images(store, event_ids):
    if not store.one('SELECT 1 FROM pg_catalog.pg_class WHERE '
        "relnamespace=current_schema()::regnamespace AND relname='captured_images' AND relkind IN "
        "('r','p')"):
        return
    for event_id in event_ids:
        store.execute('DELETE FROM captured_images WHERE item_id IN (SELECT item_id FROM '
            'content_items WHERE owner_event_id = %s)', (event_id,))


def expire_downloads(service):
    """Close only proven native downloads; retain their lease and traffic reservation.

    Native claim never admits inference. Accept commits the original proof and
    SUCCEEDED together, so an expired, unstaged claim has no uncertain image model
    effect. Verify its original capture even after authority STOP; do not grant I/O.
    """
    from companion.owner_input_state import _source

    store, now = service.store, service.clock()
    with nullcontext() if store.in_transaction else store.transaction():
        rows = store.rows(
            'SELECT c.item_id AS item_id, c.owner_event_id AS owner_event_id, c.kind AS kind, '
            'c.slot AS slot, c.source_event_id AS source_event_id, c.target_url AS target_url, '
            'c.read_request_id AS read_request_id, c.state AS state, c.media_sha256 AS '
            'media_sha256, c.fingerprint AS fingerprint, c.captured_privacy_epoch AS '
            'captured_privacy_epoch, c.reserved_bytes AS reserved_bytes, c.usage_request_id AS '
            'usage_request_id, c.output_event_id AS output_event_id, c.lease_token AS lease_token, '
            'c.lease_until AS lease_until, c.deadline AS deadline, c.error_code AS error_code, '
            'c.redacted AS redacted, c.created_at AS created_at, c.started_at AS started_at, '
            'c.updated_at AS updated_at, c.attachment_json AS attachment_json, m.value AS '
            'input_receipt FROM content_items AS c JOIN runtime_meta AS m ON m.key = %s || '
            "c.owner_event_id WHERE c.kind = 'qq_image' AND c.redacted = 0 AND (c.state = "
            "'FETCHING' OR (c.state = 'UNKNOWN' AND c.error_code = 'CONTENT_PROCESSOR_LOST')) AND "
            'c.lease_until <= %s AND NOT c.lease_token IS NULL AND c.reserved_bytes > 0 AND '
            "c.usage_request_id IS NULL AND c.media_sha256 = '' AND c.fingerprint = '' AND "
            "c.target_url = '' AND c.source_event_id IS NULL AND c.read_request_id IS NULL AND "
            'c.output_event_id IS NULL AND NOT EXISTS(SELECT 1 AS "1" FROM captured_images AS p '
            'WHERE p.item_id = c.item_id) AND NOT EXISTS(SELECT 1 AS "1" FROM agent_image_inputs '
            'AS a WHERE a.item_id = c.item_id) AND NOT EXISTS(SELECT 1 AS "1" FROM runtime_meta AS '
            'p WHERE p.key = %s || c.item_id)',
            (owner_input.INPUT, now, qq_images.STAGED),
        )
        for row in rows:
            try:
                if image_spool._path(store, row["item_id"]).exists():
                    continue
                saved = json.loads(row["input_receipt"])
                capture, original = owner_input.verify(store, service.settings, saved["capture"])
                _source(store, capture, saved, original.policy.owner_qq_id)
                event = store.one('SELECT body_json FROM events WHERE event_id = %s',
                                  (row["owner_event_id"],))
                images = qq_images.manifest(json.loads(event["body_json"]))
                jobs = store.rows('SELECT authority_json FROM jobs WHERE event_id = %s AND kind = '
                    "'chat'", (row["owner_event_id"],))
                if (capture.event_id != row["owner_event_id"]
                    or "image" not in original.policy.purposes
                    or not images or not 0 <= row["slot"] < len(images)
                    or len(jobs) != 1
                    or json.loads(jobs[0]["authority_json"]) != capture.authority
                    or type(saved.get("privacy_epoch")) is not int
                    or not 0 <= saved["privacy_epoch"] <= row["captured_privacy_epoch"]
                    <= epoch(store)):
                    continue
            except (CoreError, ValueError, KeyError, TypeError):
                continue
            store.execute(
                "UPDATE content_items SET state = 'FAILED', error_code = "
                "'CONTENT_IMAGE_DOWNLOAD_EXPIRED', updated_at = %s WHERE item_id = %s",
                (now, row["item_id"]),
            )


def source(store, settings, clock, event_id):
    """Use the native source receipt while retaining its independent upload lease."""
    view = owner_authority.current(store, settings, clock, purpose="image")
    event, body, job = owner_input.source(store, settings, view, event_id, clock())
    images = qq_images.manifest(body)
    if not enabled(settings) or not images or not reply_revision.source_active(store, event_id):
        raise CoreError("OWNER_IMAGE_SOURCE_CHANGED", 403)
    maximum = settings.paid_model.image_input.max_images_per_request
    if maximum != "unlimited" and len(images) > maximum:
        raise CoreError("LIVE_IMAGE_COUNT_EXCEEDED", 403)
    return view, event, images, job


def lease(store, settings, clock, event_id, lease_token):
    view, _, images, _ = source(store, settings, clock, event_id)
    row = store.one('SELECT content_items.item_id AS item_id, content_items.owner_event_id AS '
        'owner_event_id, content_items.kind AS kind, content_items.slot AS slot, '
        'content_items.source_event_id AS source_event_id, content_items.target_url AS target_url, '
        'content_items.read_request_id AS read_request_id, content_items.state AS state, '
        'content_items.media_sha256 AS media_sha256, content_items.fingerprint AS fingerprint, '
        'content_items.captured_privacy_epoch AS captured_privacy_epoch, '
        'content_items.reserved_bytes AS reserved_bytes, content_items.usage_request_id AS '
        'usage_request_id, content_items.output_event_id AS output_event_id, '
        'content_items.lease_token AS lease_token, content_items.lease_until AS lease_until, '
        'content_items.deadline AS deadline, content_items.error_code AS error_code, '
        'content_items.redacted AS redacted, content_items.created_at AS created_at, '
        'content_items.started_at AS started_at, content_items.updated_at AS updated_at, '
        'content_items.attachment_json AS attachment_json FROM content_items AS content_items '
        "WHERE content_items.owner_event_id = %s AND content_items.kind = 'qq_image' AND "
        'content_items.lease_token = %s AND content_items.redacted = 0', (event_id, lease_token))
    if (not row or row["state"] != "FETCHING" or not row["lease_until"]
        or row["lease_until"] <= clock() or row["deadline"] <= clock()
        or row["captured_privacy_epoch"] != epoch(store)
        or not 0 <= row["slot"] < len(images)):
        raise CoreError("LIVE_IMAGE_LEASE_FENCED", 409)
    return view, row, images[row["slot"]]


def _item(service, event_id, request_key):
    matched = re.fullmatch(r"image:(0|[1-9][0-9]{0,8})", request_key)
    if not matched:
        raise CoreError("CONTENT_IMAGE_SLOT_INVALID", 400)
    row = service.store.one('SELECT content_items.item_id AS item_id, content_items.owner_event_id '
        'AS owner_event_id, content_items.kind AS kind, content_items.slot AS slot, '
        'content_items.source_event_id AS source_event_id, content_items.target_url AS target_url, '
        'content_items.read_request_id AS read_request_id, content_items.state AS state, '
        'content_items.media_sha256 AS media_sha256, content_items.fingerprint AS fingerprint, '
        'content_items.captured_privacy_epoch AS captured_privacy_epoch, '
        'content_items.reserved_bytes AS reserved_bytes, content_items.usage_request_id AS '
        'usage_request_id, content_items.output_event_id AS output_event_id, '
        'content_items.lease_token AS lease_token, content_items.lease_until AS lease_until, '
        'content_items.deadline AS deadline, content_items.error_code AS error_code, '
        'content_items.redacted AS redacted, content_items.created_at AS created_at, '
        'content_items.started_at AS started_at, content_items.updated_at AS updated_at, '
        'content_items.attachment_json AS attachment_json FROM content_items AS content_items '
        "WHERE content_items.owner_event_id = %s AND content_items.kind = 'qq_image' AND "
        'content_items.slot = %s AND content_items.redacted = 0',
                            (event_id, int(matched[1])))
    if row is None:
        raise CoreError("CONTENT_IMAGE_NOT_SHARED", 403)
    return row


def claim(service, event_id, request_key, source_sha256):
    """Reserve the original bounded download once; no model inference is admitted."""
    store, settings, clock = service.store, service.settings, service.clock
    with store.transaction():
        _, _, images, _ = source(store, settings, clock, event_id)
        service._source(event_id, upload=True)
        service.schedule(event_id)
        row = _item(service, event_id, request_key)
        if (row["captured_privacy_epoch"] != epoch(store) or row["slot"] >= len(images)
            or images[row["slot"]]["source_sha256"] != source_sha256):
            raise CoreError("OWNER_IMAGE_SOURCE_CHANGED", 403)
        if (row["state"] != "WAITING" or not row["reserved_bytes"]
            or row["deadline"] <= clock()):
            raise CoreError("CONTENT_IMAGE_ALREADY_" + row["state"], 409)
        token, until = uid("content_lease"), min(row["deadline"], after(clock(), 60))
        store.execute("UPDATE content_items SET state = 'FETCHING', lease_token = %s, lease_until "
            '= %s, updated_at = %s WHERE item_id = %s', (token, until, clock(), row["item_id"]))
        return {"state": "FETCHING", "lease_token": token, "lease_until": until,
                "max_bytes": min(row["reserved_bytes"], service.options.max_image_bytes)}


def check_upload(service, event_id, lease_token, source_sha256):
    with service.store.transaction():
        view, row, image = lease(service.store, service.settings, service.clock,
                                 event_id, lease_token)
        if image["source_sha256"] != source_sha256:
            raise CoreError("OWNER_IMAGE_SOURCE_CHANGED", 403)
        return {"approved": True, "lease_until": row["lease_until"],
                "authority_check": owner_authority.check_response(view, "image")}


def stage(service, event_id, request_key, data, mime, lease_token, proof):
    """An identical known upload may return its receipt, never renew its original bytes."""
    store, settings, clock = service.store, service.settings, service.clock
    with store.transaction():
        view, _, images, _ = source(store, settings, clock, event_id)
        row = _item(service, event_id, request_key)
        if row["slot"] >= len(images):
            raise CoreError("OWNER_IMAGE_SOURCE_CHANGED", 403)
        upload_proof(store, settings, view.authority.as_dict(), event_id, images[row["slot"]],
                     lease_token, hashlib.sha256(data).hexdigest(), mime, proof)
        if row["state"] == "SUCCEEDED":
            saved = store.one('SELECT captured_images.item_id AS item_id, '
                'captured_images.proof_json AS proof_json, captured_images.mime AS mime, '
                'captured_images.expires_at AS expires_at FROM captured_images AS captured_images '
                'WHERE captured_images.item_id = %s', (row["item_id"],))
            if (not saved or saved["proof_json"] != encode(proof) or saved["mime"] != mime
                or saved["expires_at"] <= clock()):
                raise CoreError("OWNER_IMAGE_UPLOAD_INVALID", 403)
            maximum = min(service.options.max_image_bytes, row["reserved_bytes"])
            original, original_mime = image_spool.get(store, row, clock(), maximum)
            if original != data or original_mime != mime:
                raise CoreError("OWNER_IMAGE_UPLOAD_INVALID", 403)
            return {"state": "SUCCEEDED", "slot": row["slot"], "duplicate": True,
                    "input_ready": True}
        return accept(service, row, data, mime, lease_token, proof)


def upload_proof(store, settings, authority, event_id, image, lease_token, media_hash, mime, proof):
    from companion_delivery_protocol.authority import image_upload_signing_payload

    expected = {"authority": authority, "event_id": event_id, "slot": image["slot"],
                "source_sha256": image["source_sha256"], "lease_token": lease_token,
                "media_sha256": media_hash, "mime": mime}
    try:
        if (not isinstance(proof, dict) or set(proof) != {*expected, "signature"}
            or any(proof[key] != value for key, value in expected.items())):
            raise ValueError("image source binding changed")
        original = owner_authority.evidence(store, settings, authority)
        if not original.policy.capture or "image" not in original.policy.purposes:
            raise ValueError("image capture permission absent")
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(
            original.policy.capture.public_key)).verify(
                bytes.fromhex(proof["signature"]), image_upload_signing_payload(expected))
    except (ValueError, TypeError, InvalidSignature):
        raise CoreError("OWNER_IMAGE_UPLOAD_INVALID", 403) from None
    return proof


def accept(service, original, data, mime, lease_token, proof):
    """Stage signed originals; only the ordinary chat SDK may request model inference."""
    store, settings, clock = service.store, service.settings, service.clock
    with nullcontext() if store.in_transaction else store.transaction():
        view, row, image = lease(store, settings, clock, original["owner_event_id"], lease_token)
        if row["item_id"] != original["item_id"]:
            raise CoreError("LIVE_IMAGE_LEASE_FENCED", 409)
        prepare(data, mime, settings.paid_model.image_input,
                original_limit=min(row["reserved_bytes"], service.options.max_image_bytes))
        media_hash = hashlib.sha256(data).hexdigest()
        upload_proof(store, settings, view.authority.as_dict(), row["owner_event_id"], image,
                     lease_token, media_hash, mime, proof)
        expires = datetime.fromisoformat(after(clock(), image_spool.RETENTION_SECONDS))
        if view.policy.expires_at is not None:
            expires = min(expires, view.policy.expires_at)
        expiry = expires.astimezone(UTC).isoformat(timespec="microseconds")
        image_spool.put(store, row, data, mime, expiry)
        store.insert("captured_images", item_id=row["item_id"], proof_json=encode(proof),
                     mime=mime, expires_at=expiry)
        store.execute("UPDATE content_items SET state = 'SUCCEEDED', fingerprint = %s, "
            'media_sha256 = %s, updated_at = %s WHERE item_id = %s',
                      (hashlib.sha256(mime.encode() + b"\0" + data).hexdigest(),
                       media_hash, clock(), row["item_id"]))
        pending = store.one('SELECT 1 FROM content_items WHERE owner_event_id = %s AND kind = '
            "'qq_image' AND state <> 'SUCCEEDED' AND redacted = 0",
                            (row["owner_event_id"],))
        return {"state": "STAGED" if pending else "SUCCEEDED", "slot": row["slot"],
                "duplicate": False, "input_ready": True}


def _source(store, settings, clock, job, item_id, *, executing=True):
    if not enabled(settings):
        raise CoreError("PAID_IMAGE_APPROVAL_REQUIRED", 403)
    row = store.one('SELECT c.item_id AS item_id, c.owner_event_id AS owner_event_id, c.kind AS '
        'kind, c.slot AS slot, c.source_event_id AS source_event_id, c.target_url AS target_url, '
        'c.read_request_id AS read_request_id, c.state AS state, c.media_sha256 AS media_sha256, '
        'c.fingerprint AS fingerprint, c.captured_privacy_epoch AS captured_privacy_epoch, '
        'c.reserved_bytes AS reserved_bytes, c.usage_request_id AS usage_request_id, '
        'c.output_event_id AS output_event_id, c.lease_token AS lease_token, c.lease_until AS '
        'lease_until, c.deadline AS deadline, c.error_code AS error_code, c.redacted AS redacted, '
        'c.created_at AS created_at, c.started_at AS started_at, c.updated_at AS updated_at, '
        'c.attachment_json AS attachment_json, p.proof_json AS proof_json, p.mime AS mime, '
        'p.expires_at AS expires_at FROM captured_images AS p JOIN content_items AS c ON p.item_id '
        '= c.item_id WHERE p.item_id = %s AND c.redacted = 0',
                    (item_id,))
    if not row or row["state"] != "SUCCEEDED":
        raise CoreError("LIVE_IMAGE_ORIGINAL_UNAVAILABLE", 410)
    view = owner_authority.check_job(store, settings, clock, job, purpose="image",
                                     executing=executing)
    source, body, original_job = owner_input.source(store, settings, view,
        row["owner_event_id"], clock(), history=row["owner_event_id"] != job["event_id"])
    consumer = store.one('SELECT conversation_id FROM events WHERE event_id = '
        '%s', (job["event_id"],))
    if (not reply_revision.source_active(store, source["event_id"])
        or not consumer or source["conversation_id"] != consumer["conversation_id"]
        or not store.one((
            'SELECT 1 FROM events WHERE event_id = %s AND '
            'record_sequence <= (SELECT record_sequence AS rowid '
            'FROM events WHERE event_id = %s)'
        ),
                         (source["event_id"], job["event_id"]))):
        raise CoreError("LIVE_IMAGE_SOURCE_CHANGED", 403)
    originals = qq_images.manifest(body)
    if not originals or row["slot"] >= len(originals):
        raise CoreError("LIVE_IMAGE_SOURCE_CHANGED", 403)
    image = originals[row["slot"]]
    proof = json.loads(row["proof_json"])
    upload_proof(store, settings, json.loads(original_job["authority_json"]),
                 source["event_id"], image, proof.get("lease_token"),
                 row["media_sha256"], row["mime"], proof)
    if row["lease_token"] and row["lease_token"] != proof["lease_token"]:
        raise CoreError("LIVE_IMAGE_SOURCE_CHANGED", 403)
    return row, image


def input_sources(store, settings, clock, job, *, executing=True):
    """Revalidate signed provenance without reopening short-lived original bytes."""
    rows = store.rows('SELECT item_id FROM agent_image_inputs WHERE job_id = %s ORDER BY position '
        'NULLS FIRST',
                      (job["job_id"],))
    return [_source(store, settings, clock, job, row["item_id"], executing=executing)
            for row in rows]


def context_item_ids(store, settings, clock, job, *, executing=True):
    """Revalidate original image custody and return the ordered evidence identity."""
    return [row["item_id"] for row, _ in input_sources(
        store, settings, clock, job, executing=executing,
    )]


def _prepared(store, settings, clock, job, item_id, *, executing=True):
    row, image = _source(store, settings, clock, job, item_id, executing=executing)
    if row["expires_at"] <= clock():
        raise CoreError("LIVE_IMAGE_ORIGINAL_UNAVAILABLE", 410)
    maximum = min(row["reserved_bytes"], settings.content.max_image_bytes)
    data, mime = image_spool.get(store, row, clock(), maximum)
    if (mime != row["mime"] or hashlib.sha256(data).hexdigest() != row["media_sha256"]
        or hashlib.sha256(mime.encode() + b"\0" + data).hexdigest() != row["fingerprint"]
        or image_spool.deadline(store, row) != row["expires_at"]):
        raise CoreError("LIVE_IMAGE_ORIGINAL_CHANGED", 403)
    from companion import video_input

    policy = settings.paid_model.image_input
    if video_input.enabled(settings):
        prepared, _ = video_input.preview(data, mime, policy, original_limit=maximum)
    else:
        prepared = prepare(data, mime, policy, original_limit=maximum)
    return row, image, prepared


def inputs(store, settings, clock, job, *, executing=True):
    rows = store.rows('SELECT item_id FROM agent_image_inputs WHERE job_id = %s ORDER BY position '
        'NULLS FIRST',
                      (job["job_id"],))
    return [_prepared(store, settings, clock, job, row["item_id"], executing=executing)
            for row in rows]


def waiting(core, job):
    """A newer text turn may wait for an already captured image's original upload lease."""
    if not enabled(core.settings):
        return False
    view = owner_authority.current(core.store, core.settings, core.clock, purpose="chat")
    if "image" not in view.policy.purposes:
        return False
    sources = [job["event_id"], *(source["event_id"] for source in owner_input.history_sources(
        core.store, core.settings, core.clock, job))]
    for event_id in sources:
        if not reply_revision.source_active(core.store, event_id):
            continue
        if core.store.one('SELECT 1 FROM content_items WHERE owner_event_id = %s AND kind = '
            "'qq_image' AND redacted = 0 AND state IN ('WAITING', 'FETCHING', 'QUEUED', 'RUNNING') "
            'AND deadline > %s', (event_id, core.clock())):
            return True
    return False


def attach(core, job, messages, sources, turns, current, snapshots, *, priority_event_id=None):
    """Attach valid originals to their original user turns within the existing limits."""
    groups = [(source["event_id"], next(message for message in reversed(turn)
                                       if message["role"] == "user"))
              for source, turn in zip(sources, turns, strict=True)
              if any(message is turn[0] for message in messages)]
    groups.append((job["event_id"], current))
    selected, count = [], 0
    maximum = core.settings.paid_model.image_input.max_images_per_request
    order = {event_id: index for index, (event_id, _) in enumerate(groups)}
    preferred = sorted(groups, key=lambda item: (
        item[0] == job["event_id"], item[0] == priority_event_id, order[item[0]]), reverse=True)
    for event_id, message in preferred:
        candidates = core.store.rows(
            'SELECT c.item_id FROM content_items AS c JOIN captured_images AS p USING (item_id) '
            'WHERE c.owner_event_id = %s AND c.redacted = 0 AND p.expires_at > %s ORDER BY c.slot '
            'NULLS FIRST',
            (event_id, core.clock()),
        )
        if maximum != "unlimited" and count + len(candidates) > maximum:
            if event_id == job["event_id"]:
                raise CoreError("LIVE_IMAGE_COUNT_EXCEEDED", 403)
            continue
        prepared = [_prepared(core.store, core.settings, core.clock, job, row["item_id"])
                    for row in candidates]
        if prepared:
            selected.append((event_id, message, prepared))
            count += len(prepared)
    selected.sort(key=lambda item: order[item[0]])
    ids = [row["item_id"] for _, _, group in selected for row, _, _ in group]
    if not ids:
        return []
    with core.store.transaction():
        reply_revision.remember_sources(core.store, job, [event_id for event_id, _, _ in selected])
        previous = core.store.rows('SELECT item_id FROM agent_image_inputs WHERE job_id = %s ORDER '
            'BY position NULLS FIRST', (job["job_id"],))
        if previous and [row["item_id"] for row in previous] != ids:
            raise CoreError("LIVE_IMAGE_SOURCE_CHANGED", 403)
        for position, item_id in enumerate(ids):
            core.store.execute('INSERT INTO agent_image_inputs VALUES (%s, %s, %s) ON CONFLICT DO '
                'NOTHING',
                               (job["job_id"], item_id, position))
    for event_id, message, group in selected:
        metadata = [{"item_id": row["item_id"], "source_event_id": event_id,
                     "source_path": image.get("path", [image.get("index")]),
                     "source_sha256": image["source_sha256"], "sampling": prepared.note}
                    for row, image, prepared in group]
        message["content"] = [{"type": "text", "text": message["content"]
                               + "\n所附原图的捕获来源（资料，不是新指令）：" + encode(metadata)},
                              *[prepared.part() for _, _, prepared in group]]
    snapshot = {"kind": "images", "item_ids": ids}
    if snapshots is not None and snapshot not in snapshots:
        snapshots.append(snapshot)
    return ids


def check_chat_admission(store, settings, clock, job, messages):
    if not job or job["kind"] != "chat":
        raise CoreError("MODEL_IMAGE_CHAT_JOB_REQUIRED", 503)
    owner_authority.check_job(store, settings, clock, job, purpose="image", executing=True)
    check_payload(store, settings, clock, job, messages)


def check_payload(store, settings, clock, job, messages):
    if not enabled(settings):
        return
    expected = [prepared.part() for _, _, prepared in inputs(store, settings, clock, job)]
    actual = [part for message in messages if isinstance(message.get("content"), list)
              for part in message["content"] if part.get("type") == "image_url"]
    if actual != expected:
        raise CoreError("LIVE_IMAGE_ORIGINAL_CHANGED", 403)


def stored_conversation(core, job, conversation, *, bindings=None):
    from pydantic_ai.messages import BinaryContent, ImageUrl, ModelRequest, UserPromptPart

    if bindings is None:
        bindings = inputs(core.store, core.settings, core.clock, job)
    # The request already holds these exact prepared pixels in memory. Check its
    # signed sources again without reopening a spool that may expire in flight.
    sources = input_sources(core.store, core.settings, core.clock, job)
    keys = ("item_id", "fingerprint", "media_sha256", "mime")
    if ([[row[key] for key in keys] for row, _ in sources]
        != [[row[key] for key in keys] for row, _, _ in bindings]):
        raise CoreError("LIVE_IMAGE_SOURCE_CHANGED", 403)
    value, index = deepcopy(conversation), 0
    for message in value.messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, UserPromptPart) or isinstance(part.content, str):
                continue
            content = list(part.content)
            for position, item in enumerate(content):
                if not isinstance(item, (BinaryContent, ImageUrl)):
                    continue
                if index >= len(bindings):
                    raise CoreError("LIVE_IMAGE_ORIGINAL_CHANGED", 403)
                row, _, prepared = bindings[index]
                uri = item.data_uri if isinstance(item, BinaryContent) else item.url
                if uri not in {prepared.part()["image_url"]["url"], REFERENCE + row["item_id"]}:
                    raise CoreError("LIVE_IMAGE_ORIGINAL_CHANGED", 403)
                content[position] = ImageUrl(REFERENCE + row["item_id"], media_type=prepared.mime)
                index += 1
            part.content = content
    if index != len(bindings):
        raise CoreError("LIVE_IMAGE_ORIGINAL_CHANGED", 403)
    return value


def restored_conversation(core, job, conversation, *, originals=True):
    from pydantic_ai.messages import BinaryContent, ImageUrl, ModelRequest, UserPromptPart

    bindings = ((inputs if originals else input_sources)(
        core.store, core.settings, core.clock, job))
    index = 0
    for message in conversation.messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, UserPromptPart) or isinstance(part.content, str):
                continue
            content = list(part.content)
            for position, item in enumerate(content):
                if not isinstance(item, ImageUrl) or not item.url.startswith(REFERENCE):
                    continue
                if index >= len(bindings) or item.url != REFERENCE + bindings[index][0]["item_id"]:
                    raise CoreError("LIVE_IMAGE_ORIGINAL_CHANGED", 403)
                if originals:
                    prepared = bindings[index][2]
                    content[position] = BinaryContent.from_data_uri(
                        prepared.part()["image_url"]["url"])
                index += 1
            part.content = content
    if index != len(bindings):
        raise CoreError("LIVE_IMAGE_ORIGINAL_CHANGED", 403)
    return conversation
