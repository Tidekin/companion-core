"""Authenticated current-owner originals; source and result fences, never downloads."""

import hashlib
import json
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from companion import image_spool, paid_budget, qq_rich, qq_source
from companion.contracts import CoreError
from companion.privacy import epoch
from companion.store import encode

FROZEN = "live_text_image:"
STAGED = "live_text_image_upload:"
UPLOAD_DOMAIN = b"companion-core/onebot-owner-image/v1\x00"


def upload_proof(approval, event_id, image, lease_token, media_sha256, mime, proof):
    expected = {
        "plan_sha256": approval.plan_sha256,
        "event_id": event_id,
        "source_sha256": image["source_sha256"],
        "lease_token": lease_token,
        "media_sha256": media_sha256,
        "mime": mime,
    }
    try:
        if (
            not isinstance(proof, dict)
            or set(proof) != {*expected, "signature"}
            or any(proof[key] != value for key, value in expected.items())
        ):
            raise ValueError()
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(approval.capture_public_key)).verify(
            bytes.fromhex(proof["signature"]), UPLOAD_DOMAIN + encode(expected).encode()
        )
    except (ValueError, TypeError, InvalidSignature):
        raise CoreError("LIVE_IMAGE_UPLOAD_SIGNATURE_REQUIRED", 403) from None
    return proof


def enabled(settings):
    return bool(
        settings.live_text_window and settings.live_text_window.image_scope == "owner_image_v1"
    )


def manifest(body):
    shared = [item for item in body.get("shared_content", []) if item.get("kind") != "file"]
    images = [
        item
        for item in qq_rich.media_segments(body.get("rich"))
        if item.get("type") in {"image", "face"} and item.get("coverage") == "UNREAD"
    ]
    if not images:
        if shared:
            raise CoreError("LIVE_IMAGE_SOURCE_REQUIRED", 403)
        return None
    if (
        not 1 <= len(images) <= 64
        or any(
            image.get("slot") != slot
            or not isinstance(image.get("source_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", image["source_sha256"])
            for slot, image in enumerate(images)
        )
        or shared != [
            {"kind": "image", "slot": slot} for slot in range(len(images))
        ]
    ):
        raise CoreError("LIVE_IMAGE_SOURCE_REQUIRED", 403)
    return images


def policy(settings, approval):
    paid = settings.paid_model
    image = paid.image_input if paid else None
    if (
        approval.image_scope != "owner_image_v1"
        or image is None
        or approval.image_approval_sha256 != image.approval_sha256
        or approval.rich_input_scope != "references_v1"
        or (approval.history_scope != "none" and approval.companion_scope == "none")
        or not approval.capture_public_key
        or not settings.content_enabled
        or not settings.observation_enabled
        or not settings.content.use_chat_model_for_vision
    ):
        raise CoreError("LIVE_IMAGE_APPROVAL_REQUIRED", 403)
    adapters = settings.observation_adapters
    adapter = adapters.get(settings.content.image_adapter)
    if (
        (len(adapters) != 1 and getattr(approval, "link_scope", "none") != "owner_links_v1")
        or adapter is None
        or adapter.source != "qq_image"
        or not adapter.media_analysis
    ):
        raise CoreError("LIVE_IMAGE_ADAPTER_REQUIRED", 403)
    return image


def original_deadline(store, job):
    record = store.one('SELECT value FROM runtime_meta WHERE key = %s', (FROZEN + job["job_id"],))
    value = json.loads(record["value"]) if record else {}
    if not isinstance(value.get("original_expires_at"), str):
        raise CoreError("LIVE_IMAGE_ORIGINAL_UNAVAILABLE", 410)
    return value["original_expires_at"]


def source(store, settings, approval, event_id):
    """Pure DB proof after the caller's live-window guard; no recursive guard calls."""
    image_policy = policy(settings, approval)
    from companion import private_life, reply_revision

    if private_life.enabled(settings):
        current = store.one('SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, jobs.kind AS '
            'kind, jobs.state AS state, jobs.idempotency_key AS idempotency_key, jobs.attempts AS '
            'attempts, jobs.max_attempts AS max_attempts, jobs.lease_owner AS lease_owner, '
            'jobs.lease_until AS lease_until, jobs.next_attempt_at AS next_attempt_at, '
            'jobs.last_error_code AS last_error_code, jobs.created_at AS created_at, '
            'jobs.updated_at AS updated_at, jobs.lease_token AS lease_token, jobs.context_tracked '
            'AS context_tracked, jobs.authority_json AS authority_json FROM jobs AS jobs WHERE '
            "jobs.event_id = %s AND jobs.kind = 'chat'", (event_id,))
        if current:
            reply_revision.check_job(store, current)
    try:
        event, body, job = qq_source.source(store, settings, approval, event_id)
    except CoreError as error:
        code = "LIVE_IMAGE_SOURCE_CHANGED" if error.code == "LIVE_SOURCE_CHANGED" else (
            error.code.replace("LIVE_SOURCE_", "LIVE_IMAGE_")
        )
        raise CoreError(code, error.status) from None
    image = manifest(body)
    limits = [limit for limit in (
        image_policy.max_images_per_request, settings.content.max_images_per_event,
    ) if limit != "unlimited"]
    if image and limits and len(image) > min(limits):
        raise CoreError("LIVE_IMAGE_COUNT_EXCEEDED", 403)
    if not image:
        raise CoreError("LIVE_IMAGE_SOURCE_CHANGED", 403)
    return event, body, image, job


def _rows(store, event_id):
    return store.rows(
        'SELECT content_items.item_id AS item_id, content_items.owner_event_id AS owner_event_id, '
        'content_items.kind AS kind, content_items.slot AS slot, content_items.source_event_id AS '
        'source_event_id, content_items.target_url AS target_url, content_items.read_request_id AS '
        'read_request_id, content_items.state AS state, content_items.media_sha256 AS '
        'media_sha256, content_items.fingerprint AS fingerprint, '
        'content_items.captured_privacy_epoch AS captured_privacy_epoch, '
        'content_items.reserved_bytes AS reserved_bytes, content_items.usage_request_id AS '
        'usage_request_id, content_items.output_event_id AS output_event_id, '
        'content_items.lease_token AS lease_token, content_items.lease_until AS lease_until, '
        'content_items.deadline AS deadline, content_items.error_code AS error_code, '
        'content_items.redacted AS redacted, content_items.created_at AS created_at, '
        'content_items.started_at AS started_at, content_items.updated_at AS updated_at, '
        'content_items.attachment_json AS attachment_json FROM content_items AS content_items '
        "WHERE content_items.owner_event_id = %s AND content_items.kind = 'qq_image' AND "
        'content_items.redacted = 0 ORDER BY content_items.slot NULLS FIRST',
        (event_id,),
    )


def lease(store, settings, approval, clock, event_id, *, request_id=None, lease_token=None):
    _, _, images, job = source(store, settings, approval, event_id)
    rows = _rows(store, event_id)
    if (
        job["state"] != "QUEUED" or len(rows) != len(images)
        or any(row["kind"] != "qq_image" or row["slot"] != index
               for index, row in enumerate(rows))
    ):
        raise CoreError("LIVE_IMAGE_LEASE_FENCED", 409)
    selected = rows if request_id else [
        row for row in rows if row["lease_token"] == lease_token
    ] if lease_token else rows[:1]
    expected = "RUNNING" if request_id else "FETCHING"
    if not selected or any(
        row["state"] != expected
        or row["captured_privacy_epoch"] != epoch(store)
        or not row["lease_token"] or not row["lease_until"]
        or row["lease_until"] <= clock() or row["deadline"] <= clock()
        or (request_id and row["usage_request_id"] != request_id)
        for row in selected
    ):
        raise CoreError("LIVE_IMAGE_LEASE_FENCED", 409)
    row = selected[0]
    return row, job, images[row["slot"]]


def freeze(store, settings, approval, clock, row, messages, proofs):
    from companion.model import model_payload

    _, job, _ = lease(
        store, settings, approval, clock, row["owner_event_id"], request_id=row["usage_request_id"]
    )
    _, _, images, _ = source(store, settings, approval, row["owner_event_id"])
    rows = _rows(store, row["owner_event_id"])
    key = FROZEN + job["job_id"]
    if store.one('SELECT 1 FROM runtime_meta WHERE key = %s', (key,)):
        raise CoreError("LIVE_IMAGE_ALREADY_FROZEN", 409)
    value = {
        "plan_sha256": approval.plan_sha256,
        "privacy_epoch": epoch(store),
        "event_id": row["owner_event_id"],
        "request_id": row["usage_request_id"],
        "payload_sha256": paid_budget.sha(model_payload(settings, messages, "content_vision")),
        "image_approval_sha256": approval.image_approval_sha256,
        "original_expires_at": min(image_spool.deadline(store, item) for item in rows),
        "images": [
            {
                "item_id": item["item_id"], "source": source_image,
                "media_sha256": item["media_sha256"], "fingerprint": item["fingerprint"],
                "upload_proof": proof,
            }
            for item, source_image, proof in zip(rows, images, proofs, strict=True)
        ],
    }
    store.insert("runtime_meta", key=key, value=encode(value))
    for item in rows:
        store.execute('DELETE FROM runtime_meta WHERE key = %s', (STAGED + item["item_id"],))
    return job


def frozen(store, settings, approval, job):
    _, _, images, _ = source(store, settings, approval, job["event_id"])
    record = store.one('SELECT value FROM runtime_meta WHERE key = %s', (FROZEN + job["job_id"],))
    value = json.loads(record["value"]) if record else {}
    rows = _rows(store, job["event_id"])
    if (
        value.get("privacy_epoch") != epoch(store)
        or value.get("plan_sha256") != approval.plan_sha256
        or value.get("event_id") != job["event_id"]
        or value.get("image_approval_sha256") != approval.image_approval_sha256
        or len(value.get("images", [])) != len(images) or len(rows) != len(images)
    ):
        raise CoreError("LIVE_IMAGE_SOURCE_CHANGED", 403)
    for item, row, image in zip(value["images"], rows, images, strict=True):
        if (
            row["kind"] != "qq_image" or row["slot"] != image["slot"]
            or row["captured_privacy_epoch"] != epoch(store)
            or item.get("item_id") != row["item_id"] or item.get("source") != image
            or value.get("request_id") != row["usage_request_id"]
            or item.get("media_sha256") != row["media_sha256"]
            or item.get("fingerprint") != row["fingerprint"]
        ):
            raise CoreError("LIVE_IMAGE_SOURCE_CHANGED", 403)
        proof = item.get("upload_proof") or {}
        if row["state"] == "RUNNING" and proof.get("lease_token") != row["lease_token"]:
            raise CoreError("LIVE_IMAGE_LEASE_FENCED", 409)
        upload_proof(
            approval, job["event_id"], image, proof.get("lease_token"),
            row["media_sha256"], proof.get("mime"), proof,
        )
    return value, rows[0]


def result(store, settings, approval, job):
    """Every native observation and the shared paid admission, rehashed at each boundary."""
    value, _ = frozen(store, settings, approval, job)
    admission = store.one(
        'SELECT a.request_id AS request_id, a.logical_request_id AS logical_request_id, '
        'a.attempt_id AS attempt_id, a.attempt_number AS attempt_number, a.window_id AS window_id, '
        'a.contract_sha256 AS contract_sha256, a.payload_sha256 AS payload_sha256, a.snapshot_json '
        'AS snapshot_json, a.reserved_micro_usd AS reserved_micro_usd, a.input_bound AS '
        'input_bound, a.output_bound AS output_bound, a.lease_token AS lease_token, a.lease_until '
        'AS lease_until, a.io_started_at AS io_started_at, a.journal_path AS journal_path, '
        'a.journal_id AS journal_id, a.measurement_json AS measurement_json, a.authority_json AS '
        'authority_json, m.state AS state FROM model_admissions AS a JOIN model_usage AS m ON '
        'a.request_id = m.request_id WHERE a.request_id = %s', (value["request_id"],),
    )
    if (
        not admission or admission["state"] != "SETTLED"
        or admission["logical_request_id"] != job["job_id"]
        or admission["payload_sha256"] != value["payload_sha256"]
    ):
        raise CoreError("LIVE_IMAGE_RESULT_REQUIRED", 403)
    evidence_rows = []
    for item, row in zip(value["images"], _rows(store, job["event_id"]), strict=True):
        observation = store.one(
            'SELECT o.event_id AS event_id, o.adapter_id AS adapter_id, o.source_kind AS '
            'source_kind, o.result_status AS result_status, o.source_sha256 AS source_sha256, '
            'o.canonical_url AS canonical_url, o.device_id AS device_id, o.session_id AS '
            'session_id, o.consent_id AS consent_id, o.evidence_json AS evidence_json, '
            'o.captured_privacy_epoch AS captured_privacy_epoch, o.redacted AS redacted, '
            'o.created_at AS created_at, e.body_json AS body_json, e.redacted AS event_redacted '
            'FROM observations AS o JOIN events AS e ON o.event_id = e.event_id WHERE o.event_id = '
            '%s',
            (row["output_event_id"],),
        )
        dependencies = store.rows(
            'SELECT source_event_id FROM event_dependencies WHERE event_id = %s ORDER BY '
            'source_event_id NULLS FIRST', (row["output_event_id"],),
        )
        if (
            row["state"] != "SUCCEEDED" or not observation
            or observation["redacted"] or observation["event_redacted"]
            or observation["source_kind"] != "image_analysis"
            or observation["result_status"] != "FETCHED"
            or dependencies != [{"source_event_id": job["event_id"]}]
        ):
            raise CoreError("LIVE_IMAGE_RESULT_REQUIRED", 403)
        body = json.loads(observation["body_json"])
        evidence = json.loads(observation["evidence_json"])
        if (
            evidence.get("media_sha256") != item["media_sha256"]
            or observation["captured_privacy_epoch"] != epoch(store)
            or observation["source_sha256"]
            != hashlib.sha256(body.get("text", "").encode()).hexdigest()
        ):
            raise CoreError("LIVE_IMAGE_RESULT_CHANGED", 403)
        evidence_rows.append({
            "slot": row["slot"], "item_sha256": paid_budget.sha(row),
            "observation_sha256": paid_budget.sha(observation),
        })
    result = {"image": value, "items": evidence_rows,
              "admission_sha256": paid_budget.sha(admission)}
    if "video" in value:
        from companion import qq_video

        result["video"] = qq_video.result(store, settings, approval, job)
    return result


def retrieval_text(store, settings, clock, job):
    """Verified observations are search clues; never substitute for approved memory evidence."""
    result(store, settings, settings.live_text_window, job)
    texts = []
    for row in _rows(store, job["event_id"]):
        observed = store.one(
            'SELECT body_json FROM events WHERE event_id = %s', (row["output_event_id"],),
        )
        text = json.loads(observed["body_json"]).get("text", "")
        if text not in texts:
            texts.append(text)
    return "\n".join(texts)[:6000]


def original_parts(store, settings, clock, job, *, skip_slots=frozenset()):
    """`skip_slots` are images inside quotes the owner has since erased or recalled."""
    from companion.image_frames import prepare

    result(store, settings, settings.live_text_window, job)
    parts = []
    maximum = min(
        settings.content.max_image_bytes, settings.paid_model.image_input.max_bytes_per_image,
    )
    for row in _rows(store, job["event_id"]):
        if row["slot"] in skip_slots:
            continue
        data, mime = image_spool.get(store, row, clock(), maximum)
        if (
            hashlib.sha256(data).hexdigest() != row["media_sha256"]
            or hashlib.sha256(mime.encode() + b"\0" + data).hexdigest() != row["fingerprint"]
        ):
            raise CoreError("LIVE_IMAGE_ORIGINAL_CHANGED", 403)
        if settings.paid_model.video_input:
            from companion.video_input import preview

            prepared, _ = preview(data, mime, settings.paid_model.image_input)
        else:
            prepared = prepare(data, mime, settings.paid_model.image_input)
        parts.append(prepared.part())
    return parts
