"""Authenticated owner file manifests and upload proofs; never fetch or parse bytes."""

import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from companion import qq_rich, qq_source, reply_revision
from companion.contracts import CoreError
from companion.file_evidence import safe_file_id, safe_filename
from companion.store import encode

STAGED = "live_text_file_upload:"
UPLOAD_DOMAIN = b"companion-core/onebot-owner-file/v1\x00"


def manifest(body):
    files = [item for item in qq_rich.media_segments(body.get("rich"))
             if item.get("type") == "file" and item.get("coverage") == "UNREAD"]
    shared = [item for item in body.get("shared_content", []) if item.get("kind") == "file"]
    if not files and not shared:
        return []
    try:
        if not 1 <= len(files) <= 64 or shared != [
            {"kind": "file", "slot": slot} for slot in range(len(files))
        ]:
            raise ValueError()
        previous_index = -1
        for slot, item in enumerate(files):
            nested = "path" in item
            if nested:
                qq_rich.validate_media(item, item["index"])
            if (set(item) != {"index", "type", "actor", "coverage", "slot", "file_id",
                              "filename", "size", "source_sha256"} | (
                                  {"path", "origin"} if nested else set())
                or item["actor"] != "UNVERIFIED" or type(item["slot"]) is not int
                or item["slot"] != slot or type(item["index"]) is not int
                or not 0 <= item["index"] < 64
                or (not nested and item["index"] <= previous_index)
                or not isinstance(item["source_sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", item["source_sha256"])
                or (item["size"] is not None and (
                    type(item["size"]) is not int or not 1 <= item["size"] <= 2**53 - 1))):
                raise ValueError()
            safe_file_id(item["file_id"])
            safe_filename(item["filename"])
            if not nested:
                previous_index = item["index"]
    except (ValueError, KeyError, TypeError):
        raise CoreError("PRIVATE_FILE_MANIFEST_INVALID", 403) from None
    return files


def policy(settings, approval):
    if (getattr(approval, "file_scope", "none") != "owner_file_v1"
        or approval.rich_input_scope != "references_v1"
        or approval.companion_scope != "owner_private_v1"
        or not approval.capture_public_key
        or not settings.content_enabled or not settings.observation_enabled):
        raise CoreError("PRIVATE_FILE_SCOPE_REQUIRED", 403)


def source(store, settings, approval, event_id, *, item_id=None):
    """The caller owns live-window guarding; an explicit current follow-up may consume a file."""
    policy(settings, approval)
    event, body, job = qq_source.source(store, settings, approval, event_id)
    if not reply_revision.source_active(store, event_id):
        raise CoreError("REPLY_SOURCE_RECALLED", 409)
    files = manifest(body)
    if not files:
        raise CoreError("PRIVATE_FILE_SOURCE_REQUIRED", 403)
    try:
        reply_revision.check_job(store, job)
    except CoreError:
        if not item_id:
            raise
        from companion.reading import followup_item

        consumer = store.one(
            'SELECT e.event_id FROM events AS e JOIN conversations AS c USING (conversation_id) '
            'WHERE e.conversation_id = %s AND e.reply_revision = c.reply_revision AND e.redacted = '
            '0',
            (event["conversation_id"],),
        )
        inherited = followup_item(store, settings, consumer["event_id"]) if consumer else None
        if not inherited or (inherited["item_id"], inherited["owner_event_id"]) != (
            item_id, event_id,
        ):
            raise
        _, _, job = qq_source.source(store, settings, approval, consumer["event_id"])
        reply_revision.check_job(store, job)
    return event, body, files, job


def upload_proof(approval, event_id, original, lease_token, media_sha256, mime, size, proof):
    from companion.owner_authority import OwnerAuthorityView

    native = isinstance(approval, OwnerAuthorityView)
    binding = ({"authority": approval.authority.as_dict()} if native else
               {"plan_sha256": approval.plan_sha256})
    expected = {**binding, "event_id": event_id,
                "slot": original["slot"], "source_sha256": original["source_sha256"],
                "lease_token": lease_token, "file_id": original["file_id"],
                "filename": original["filename"], "media_sha256": media_sha256,
                "mime": mime, "bytes": size}
    try:
        if (not isinstance(proof, dict) or set(proof) != {*expected, "signature"}
            or any(type(proof[key]) is not type(value) or proof[key] != value
                   for key, value in expected.items())):
            raise ValueError()
        if native:
            from companion_delivery_protocol.authority import file_upload_signing_payload

            if not approval.policy.capture or "file" not in approval.policy.purposes:
                raise ValueError("file capture permission absent")
            public_key = approval.policy.capture.public_key
            payload = file_upload_signing_payload(expected)
        else:
            public_key = approval.capture_public_key
            payload = UPLOAD_DOMAIN + encode(expected).encode()
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
            bytes.fromhex(proof["signature"]), payload)
    except (ValueError, TypeError, InvalidSignature):
        raise CoreError("PRIVATE_FILE_UPLOAD_SIGNATURE_REQUIRED", 403) from None
    return proof
