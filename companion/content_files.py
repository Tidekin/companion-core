"""Private-file branches of ContentService; use its rows, observations and worker."""

import asyncio
import hashlib
import json
import re
import threading
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path

from companion import file_parser, file_parser_client, image_spool, qq_files
from companion.contracts import CoreError
from companion.file_evidence import DocumentEvidence
from companion.file_evidence import PrivateFilesConfig as PrivateFilesConfig
from companion.privacy import epoch
from companion.store import after, encode, uid
from companion.transport import digest


def _config(service, config):
    value = config if config is not None else getattr(service.settings, "private_files", None)
    return PrivateFilesConfig.model_validate(value or {})


def _transaction(store):
    return nullcontext() if store.in_transaction else store.transaction()


def _guard(service, config, event_id, *, upload=False, item_id=None):
    from companion import live_text, owner_authority, owner_input, reply_revision, runtime_config

    if item_id is not None:
        runtime_config.get(service.store, service.settings, "content:" + item_id)

    if not config.enabled:
        raise CoreError("PRIVATE_FILES_DISABLED", 403)
    if owner_authority.required(service.store, service.settings):
        approval = owner_authority.current(
            service.store, service.settings, service.clock, purpose="file"
        )
        event, body, job = owner_input.source(
            service.store, service.settings, approval, event_id, service.clock()
        )
        files = qq_files.manifest(body)
        if (
            not service.settings.content_enabled
            or not service.settings.observation_enabled
            or not files
            or not reply_revision.source_active(service.store, event_id)
        ):
            raise CoreError("PRIVATE_FILE_SOURCE_REQUIRED", 403)
    else:
        approval, _, _ = live_text.guard(service.store, service.settings, service.clock)
        event, body, files, job = qq_files.source(
            service.store,
            service.settings,
            approval,
            event_id,
            item_id=item_id,
        )
    if upload and after(event["ingested_at"], config.upload_window_seconds) <= service.clock():
        raise CoreError("PRIVATE_FILE_UPLOAD_EXPIRED", 410)
    return event, body, files, job, approval


def _attachment(original, config):
    return {
        "version": 1,
        "index": original["index"],
        "slot": original["slot"],
        "file_id": original["file_id"],
        "filename": original["filename"],
        "declared_bytes": original["size"],
        "source_sha256": original["source_sha256"],
        "max_bytes": config.max_file_bytes,
        "spool_retention_seconds": config.spool_retention_seconds,
        "parser_image": config.parser_image,
        "parser_socket": config.parser_socket,
        "parser_limits": asdict(config.limits),
        "mime_type": "",
        "actual_bytes": 0,
    }


def _snapshot(row, original, config):
    try:
        value = json.loads(row["attachment_json"])
        expected = _attachment(original, config)
        if (
            set(value) != set(expected)
            or any(
                value[key] != item or type(value[key]) is not type(item)
                for key, item in expected.items()
                if key not in {"mime_type", "actual_bytes"}
            )
            or not isinstance(value["mime_type"], str)
            or type(value["actual_bytes"]) is not int
            or not 0 <= value["actual_bytes"] <= value["max_bytes"]
        ):
            raise ValueError()
        return value
    except (ValueError, KeyError, TypeError):
        raise CoreError("PRIVATE_FILE_SNAPSHOT_CHANGED", 409) from None


def _row(service, event_id, request_key):
    matched = re.fullmatch(r"file:(0|[1-9][0-9]{0,2})", request_key)
    if not matched:
        raise CoreError("PRIVATE_FILE_SLOT_INVALID", 400)
    row = service.store.one(
        "SELECT content_items.item_id AS item_id, content_items.owner_event_id AS owner_event_id, "
        "content_items.kind AS kind, content_items.slot AS slot, content_items.source_event_id AS "
        "source_event_id, content_items.target_url AS target_url, content_items.read_request_id AS "
        "read_request_id, content_items.state AS state, content_items.media_sha256 AS "
        "media_sha256, content_items.fingerprint AS fingerprint, "
        "content_items.captured_privacy_epoch AS captured_privacy_epoch, "
        "content_items.reserved_bytes AS reserved_bytes, content_items.usage_request_id AS "
        "usage_request_id, content_items.output_event_id AS output_event_id, "
        "content_items.lease_token AS lease_token, content_items.lease_until AS lease_until, "
        "content_items.deadline AS deadline, content_items.error_code AS error_code, "
        "content_items.redacted AS redacted, content_items.created_at AS created_at, "
        "content_items.started_at AS started_at, content_items.updated_at AS updated_at, "
        "content_items.attachment_json AS attachment_json FROM content_items AS content_items "
        "WHERE content_items.owner_event_id = %s AND content_items.kind = 'qq_file' AND "
        "content_items.slot = %s",
        (event_id, int(matched[1])),
    )
    if not row or row["redacted"] or row["captured_privacy_epoch"] != epoch(service.store):
        raise CoreError("PRIVATE_FILE_NOT_SHARED", 403)
    return row


def _original(files, row):
    if type(row["slot"]) is not int or not 0 <= row["slot"] < len(files):
        raise CoreError("PRIVATE_FILE_SOURCE_CHANGED", 409)
    return files[row["slot"]]


def schedule_files(service, event_id, *, config=None):
    config = _config(service, config)
    if not config.enabled:
        return []
    source = service.store.one("SELECT body_json FROM events WHERE event_id = %s", (event_id,))
    if not source or not qq_files.manifest(json.loads(source["body_json"])):
        return []
    s, now = service.store, service.clock()
    with _transaction(s):
        event, _, files, _, _ = _guard(service, config, event_id)
        day, _ = service._periods()
        for original in files:
            if s.one(
                "SELECT 1 FROM content_items WHERE owner_event_id = %s AND kind = 'qq_file' "
                "AND slot = %s",
                (event_id, original["slot"]),
            ):
                continue
            error = None
            if original["slot"] >= config.max_files_per_event:
                error = "PRIVATE_FILE_COUNT_LIMIT"
            elif Path(original["filename"]).suffix.lower() not in file_parser.MIMES:
                error = "PRIVATE_FILE_TYPE_UNSUPPORTED"
            elif original["size"] is not None and original["size"] > config.max_file_bytes:
                error = "PRIVATE_FILE_TOO_LARGE"
            adapter = service.settings.observation_adapters.get(config.adapter_id)
            if not error and (adapter is None or adapter.source != "qq_file"):
                error = "PRIVATE_FILE_ADAPTER_UNAVAILABLE"
            traffic = s.one(
                (
                    "SELECT COUNT(*) AS n,COALESCE(SUM(reserved_bytes),0) AS"
                    " bytes FROM content_items WHERE kind='qq_file' AND "
                    "COALESCE(started_at,created_at)>=%s AND "
                    "reserved_bytes>0"
                ),
                (day,),
            )
            if not error and (
                traffic["n"] >= config.max_daily_files
                or traffic["bytes"] + config.max_file_bytes > config.max_daily_bytes
            ):
                error = "PRIVATE_FILE_TRAFFIC_EXHAUSTED"
            s.insert(
                "content_items",
                item_id=uid("content"),
                owner_event_id=event_id,
                kind="qq_file",
                slot=original["slot"],
                state="FAILED" if error else "WAITING",
                captured_privacy_epoch=epoch(s),
                reserved_bytes=0 if error else config.max_file_bytes,
                started_at=now if not error else None,
                deadline=after(event["ingested_at"], config.wait_seconds),
                attachment_json=encode(_attachment(original, config)),
                error_code=error,
                created_at=now,
                updated_at=now,
            )
    return s.rows(
        "SELECT content_items.item_id AS item_id, content_items.owner_event_id AS "
        "owner_event_id, content_items.kind AS kind, content_items.slot AS slot, "
        "content_items.source_event_id AS source_event_id, content_items.target_url AS target_url, "
        "content_items.read_request_id AS read_request_id, content_items.state AS state, "
        "content_items.media_sha256 AS media_sha256, content_items.fingerprint AS fingerprint, "
        "content_items.captured_privacy_epoch AS captured_privacy_epoch, "
        "content_items.reserved_bytes AS reserved_bytes, content_items.usage_request_id AS "
        "usage_request_id, content_items.output_event_id AS output_event_id, "
        "content_items.lease_token AS lease_token, content_items.lease_until AS lease_until, "
        "content_items.deadline AS deadline, content_items.error_code AS error_code, "
        "content_items.redacted AS redacted, content_items.created_at AS created_at, "
        "content_items.started_at AS started_at, content_items.updated_at AS updated_at, "
        "content_items.attachment_json AS attachment_json FROM content_items AS content_items "
        "WHERE content_items.owner_event_id = %s AND content_items.kind = 'qq_file' ORDER BY "
        "content_items.slot NULLS FIRST",
        (event_id,),
    )


def claim(service, event_id, request_key, source_sha256, *, config=None):
    config = _config(service, config)
    schedule_files(service, event_id, config=config)
    with _transaction(service.store):
        _, _, files, _, _ = _guard(service, config, event_id, upload=True)
        row = _row(service, event_id, request_key)
        original = _original(files, row)
        snapshot = _snapshot(row, original, config)
        if original["source_sha256"] != source_sha256:
            raise CoreError("PRIVATE_FILE_SOURCE_CHANGED", 403)
        if row["state"] != "WAITING" or not row["reserved_bytes"]:
            raise CoreError("PRIVATE_FILE_ALREADY_" + row["state"], 409)
        if row["deadline"] <= service.clock():
            raise CoreError("PRIVATE_FILE_WAIT_EXPIRED", 410)
        from companion import runtime_config

        runtime_config.bind(
            service.store, service.settings, service.clock, "content:" + row["item_id"]
        )
        token = uid("content_lease")
        until = min(row["deadline"], after(service.clock(), config.upload_lease_seconds))
        service.store.execute(
            "UPDATE content_items SET state = 'FETCHING', lease_token = %s, lease_until = %s, "
            "updated_at = %s WHERE item_id = %s",
            (token, until, service.clock(), row["item_id"]),
        )
        return {
            "state": "FETCHING",
            "lease_token": token,
            "lease_until": until,
            "max_bytes": snapshot["max_bytes"],
        }


def _upload_lease(service, config, event_id, lease_token, source_sha256):
    _, _, files, _, approval = _guard(service, config, event_id, upload=True)
    row = service.store.one(
        "SELECT content_items.item_id AS item_id, content_items.owner_event_id "
        "AS owner_event_id, content_items.kind AS kind, content_items.slot AS slot, "
        "content_items.source_event_id AS source_event_id, content_items.target_url AS target_url, "
        "content_items.read_request_id AS read_request_id, content_items.state AS state, "
        "content_items.media_sha256 AS media_sha256, content_items.fingerprint AS fingerprint, "
        "content_items.captured_privacy_epoch AS captured_privacy_epoch, "
        "content_items.reserved_bytes AS reserved_bytes, content_items.usage_request_id AS "
        "usage_request_id, content_items.output_event_id AS output_event_id, "
        "content_items.lease_token AS lease_token, content_items.lease_until AS lease_until, "
        "content_items.deadline AS deadline, content_items.error_code AS error_code, "
        "content_items.redacted AS redacted, content_items.created_at AS created_at, "
        "content_items.started_at AS started_at, content_items.updated_at AS updated_at, "
        "content_items.attachment_json AS attachment_json FROM content_items AS content_items "
        "WHERE content_items.owner_event_id = %s AND content_items.kind = 'qq_file' AND "
        "content_items.lease_token = %s",
        (event_id, lease_token),
    )
    if (
        not row
        or row["redacted"]
        or row["captured_privacy_epoch"] != epoch(service.store)
        or row["state"] != "FETCHING"
        or row["lease_until"] <= service.clock()
        or row["deadline"] <= service.clock()
        or row["slot"] >= len(files)
    ):
        raise CoreError("PRIVATE_FILE_LEASE_FENCED", 409)
    original = _original(files, row)
    if source_sha256 != original["source_sha256"]:
        raise CoreError("PRIVATE_FILE_SOURCE_CHANGED", 403)
    return row, original, approval, _snapshot(row, original, config)


def check(service, event_id, lease_token, source_sha256, *, config=None):
    config = _config(service, config)
    with _transaction(service.store):
        row, _, _, _ = _upload_lease(service, config, event_id, lease_token, source_sha256)
        return {"approved": True, "lease_until": row["lease_until"]}


def _media(data, mime_type, snapshot):
    if not isinstance(data, bytes) or not data or len(data) > snapshot["max_bytes"]:
        raise CoreError("PRIVATE_FILE_SIZE_INVALID", 413)
    if snapshot["declared_bytes"] is not None and len(data) != snapshot["declared_bytes"]:
        raise CoreError("PRIVATE_FILE_SIZE_MISMATCH", 400)
    if (
        not isinstance(mime_type, str)
        or len(mime_type) > 256
        or re.search(r"[\x00-\x1f\x7f]", mime_type)
    ):
        raise CoreError("PRIVATE_FILE_MIME_INVALID", 415)
    mime = mime_type.split(";", 1)[0].strip().lower()
    allowed = file_parser.MIMES.get(Path(snapshot["filename"]).suffix.lower(), set())
    if mime not in allowed | {"application/octet-stream"}:
        raise CoreError("PRIVATE_FILE_MIME_INVALID", 415)
    return mime, hashlib.sha256(data).hexdigest()


def stage_file(service, event_id, request_key, data, mime_type, lease_token, proof, *, config=None):
    config = _config(service, config)
    s = service.store
    failure = None
    with _transaction(s):
        _, _, files, _, approval = _guard(service, config, event_id, upload=True)
        row = _row(service, event_id, request_key)
        original = _original(files, row)
        snapshot = _snapshot(row, original, config)
        mime, sha = _media(data, mime_type, snapshot)
        qq_files.upload_proof(
            approval, event_id, original, lease_token, sha, mime, len(data), proof
        )
        snapshot.update(mime_type=mime, actual_bytes=len(data))
        fingerprint = digest({"attachment": snapshot, "sha256": sha, "proof": proof})
        if row["state"] in {"QUEUED", "RUNNING", "SUCCEEDED"}:
            if row["fingerprint"] != fingerprint or row["media_sha256"] != sha:
                raise CoreError("IDEMPOTENCY_CONFLICT", 409)
            return {"item_id": row["item_id"], "state": row["state"], "duplicate": True}
        _upload_lease(service, config, event_id, lease_token, original["source_sha256"])
        try:
            image_spool.put(
                s, row, data, mime, after(service.clock(), snapshot["spool_retention_seconds"])
            )
        except OSError:
            s.execute(
                "UPDATE content_items SET state = 'UNKNOWN', error_code = %s, lease_token = "
                "NULL, lease_until = NULL, updated_at = %s WHERE item_id = %s",
                ("PRIVATE_FILE_ORIGINAL_AMBIGUOUS", service.clock(), row["item_id"]),
            )
            failure = CoreError("PRIVATE_FILE_ORIGINAL_AMBIGUOUS", 409)
        else:
            s.execute(
                "INSERT INTO runtime_meta (key, value) VALUES (%s, %s)",
                (qq_files.STAGED + row["item_id"], encode(proof)),
            )
            s.execute(
                "UPDATE content_items SET state = 'QUEUED', media_sha256 = %s, fingerprint = "
                "%s, attachment_json = %s, lease_token = NULL, lease_until = NULL, updated_at = %s "
                "WHERE item_id = %s",
                (sha, fingerprint, encode(snapshot), service.clock(), row["item_id"]),
            )
    if failure:
        raise failure
    return {"item_id": row["item_id"], "state": "QUEUED", "duplicate": False}


def _discard(service, item_id):
    image_spool.discard(service.store, [item_id])
    service.store.execute("DELETE FROM runtime_meta WHERE key = %s", (qq_files.STAGED + item_id,))


def _failed(service, row, code, *, state="FAILED"):
    service.store.execute(
        "UPDATE content_items SET state = %s, error_code = %s, lease_token = NULL, lease_until = "
        "NULL, updated_at = %s WHERE item_id = %s AND state <> 'ERASED' AND redacted = 0",
        (state, code, service.clock(), row["item_id"]),
    )
    _discard(service, row["item_id"])


def expire_downloads(service, *, config=None):
    """Retire only expired native file claims with proof that no bytes were staged.

    Keep the original lease and traffic reservation. Parser/upload ambiguity and
    legacy claims retain their existing recovery path; this never grants new I/O.
    """
    from companion import owner_input
    from companion.owner_input_state import _source

    config = _config(service, config)
    s, now = service.store, service.clock()
    completed = False
    with _transaction(s):
        rows = s.rows(
            "SELECT c.item_id AS item_id, c.owner_event_id AS owner_event_id, c.kind AS kind, "
            "c.slot AS slot, c.source_event_id AS source_event_id, c.target_url AS target_url, "
            "c.read_request_id AS read_request_id, c.state AS state, c.media_sha256 AS "
            "media_sha256, c.fingerprint AS fingerprint, c.captured_privacy_epoch AS "
            "captured_privacy_epoch, c.reserved_bytes AS reserved_bytes, c.usage_request_id AS "
            "usage_request_id, c.output_event_id AS output_event_id, c.lease_token AS lease_token, "
            "c.lease_until AS lease_until, c.deadline AS deadline, c.error_code AS error_code, "
            "c.redacted AS redacted, c.created_at AS created_at, c.started_at AS started_at, "
            "c.updated_at AS updated_at, c.attachment_json AS attachment_json, m.value AS "
            "input_receipt FROM content_items AS c JOIN runtime_meta AS m ON m.key = %s || "
            "c.owner_event_id WHERE c.kind = 'qq_file' AND c.redacted = 0 AND c.state = 'FETCHING' "
            "AND c.lease_until <= %s AND NOT c.lease_token IS NULL AND c.reserved_bytes > 0 AND "
            "c.usage_request_id IS NULL AND c.media_sha256 = '' AND c.fingerprint = '' AND "
            'c.output_event_id IS NULL AND NOT EXISTS(SELECT 1 AS "1" FROM runtime_meta AS p WHERE '
            "p.key = %s || c.item_id)",
            (owner_input.INPUT, now, qq_files.STAGED),
        )
        for row in rows:
            try:
                saved = json.loads(row["input_receipt"])
                capture, original = owner_input.verify(s, service.settings, saved["capture"])
                _source(s, capture, saved, original.policy.owner_qq_id)
                event = s.one(
                    "SELECT body_json FROM events WHERE event_id = %s", (row["owner_event_id"],)
                )
                files = qq_files.manifest(json.loads(event["body_json"]))
                jobs = s.rows(
                    "SELECT authority_json FROM jobs WHERE event_id = %s AND kind = 'chat'",
                    (row["owner_event_id"],),
                )
                if (
                    capture.event_id != row["owner_event_id"]
                    or "file" not in original.policy.purposes
                    or not files
                    or len(jobs) != 1
                    or json.loads(jobs[0]["authority_json"]) != capture.authority
                    or type(saved.get("privacy_epoch")) is not int
                    or not 0 <= saved["privacy_epoch"] <= row["captured_privacy_epoch"] <= epoch(s)
                    or not re.fullmatch(r"content_lease_[a-f0-9]{32}", row["lease_token"])
                ):
                    continue
                snapshot = _snapshot(row, _original(files, row), config)
                if (
                    snapshot["mime_type"]
                    or snapshot["actual_bytes"]
                    or row["reserved_bytes"] != snapshot["max_bytes"]
                    or image_spool._path(s, row["item_id"]).exists()
                ):
                    continue
            except (CoreError, ValueError, KeyError, TypeError, OSError):
                continue
            s.execute(
                "UPDATE content_items SET state = 'FAILED', error_code = "
                "'PRIVATE_FILE_DOWNLOAD_EXPIRED', updated_at = %s WHERE item_id = %s",
                (now, row["item_id"]),
            )
            completed = True
    return completed


def _record(service, row, snapshot, result, config):
    from companion.observation import ObservationEvent, ObservationPayload

    document = DocumentEvidence.from_parser(
        result,
        file_id=snapshot["file_id"],
        source_sha256=snapshot["source_sha256"],
        limits=file_parser.Limits(**snapshot["parser_limits"]),
    )
    readable = bool(result["text"].strip())
    observed = service.observations.append(
        config.adapter_id,
        ObservationEvent(
            schema_version=2,
            event_id=uid("observation"),
            event_type="content.observed",
            source="qq_file",
            source_kind="text",
            provider_event_id=row["item_id"],
            occurred_at=service.clock(),
            trace_id=row["item_id"],
            result_status="FETCHED" if readable else "UNKNOWN",
            source_event_ids=[row["owner_event_id"]],
            source_sha256=result["text_sha256"],
            privacy_epoch=row["captured_privacy_epoch"],
            payload=ObservationPayload(
                title=snapshot["filename"],
                text=result["text"],
                media_sha256=row["media_sha256"],
                processor_id=result["parser_version"],
                document=document,
                error_code=None if readable else "PRIVATE_FILE_TEXT_UNAVAILABLE",
            ),
        ),
    )
    service.store.execute(
        "UPDATE content_items SET state = 'SUCCEEDED', output_event_id = %s, lease_token = NULL, "
        "lease_until = NULL, error_code = %s, updated_at = %s WHERE item_id = %s",
        (
            observed["event_id"],
            None if readable else "PRIVATE_FILE_TEXT_UNAVAILABLE",
            service.clock(),
            row["item_id"],
        ),
    )


async def run_file_once(service, *, config=None, parse=None):
    from companion.worker_schedule import claim_scope

    config = _config(service, config)
    if not config.enabled:
        return False
    with claim_scope(service.store):
        if expire_downloads(service, config=config):
            return True
    s, now = service.store, service.clock()
    with claim_scope(s), _transaction(s):
        lost = s.one(
            "SELECT content_items.item_id AS item_id, content_items.owner_event_id AS "
            "owner_event_id, content_items.kind AS kind, content_items.slot AS slot, "
            "content_items.source_event_id AS source_event_id, content_items.target_url AS "
            "target_url, content_items.read_request_id AS read_request_id, content_items.state AS "
            "state, content_items.media_sha256 AS media_sha256, content_items.fingerprint AS "
            "fingerprint, content_items.captured_privacy_epoch AS captured_privacy_epoch, "
            "content_items.reserved_bytes AS reserved_bytes, content_items.usage_request_id AS "
            "usage_request_id, content_items.output_event_id AS output_event_id, "
            "content_items.lease_token AS lease_token, content_items.lease_until AS lease_until, "
            "content_items.deadline AS deadline, content_items.error_code AS error_code, "
            "content_items.redacted AS redacted, content_items.created_at AS created_at, "
            "content_items.started_at AS started_at, content_items.updated_at AS updated_at, "
            "content_items.attachment_json AS attachment_json FROM content_items AS content_items "
            "WHERE content_items.kind = 'qq_file' AND content_items.redacted = 0 AND "
            "content_items.state IN ('FETCHING', 'RUNNING') AND content_items.lease_until <= %s "
            "ORDER BY record_sequence NULLS FIRST LIMIT 1",
            (now,),
        )
        if lost:
            if lost["state"] == "FETCHING" and s.one(
                "SELECT 1 FROM runtime_meta WHERE key = %s",
                ("owner_input:" + lost["owner_event_id"],),
            ):
                # Expiry could not prove a pure native download. Keep original
                # lease/proof/accounting metadata; normal spool cleanup still
                # removes UNKNOWN bytes without extending their retention.
                s.execute(
                    "UPDATE content_items SET state = 'UNKNOWN', error_code = "
                    "'PRIVATE_FILE_PROCESSOR_LOST', updated_at = %s WHERE item_id = %s",
                    (now, lost["item_id"]),
                )
            else:
                _failed(service, lost, "PRIVATE_FILE_PROCESSOR_LOST", state="UNKNOWN")
            return True
        row = s.one(
            "SELECT content_items.item_id AS item_id, content_items.owner_event_id AS "
            "owner_event_id, content_items.kind AS kind, content_items.slot AS slot, "
            "content_items.source_event_id AS source_event_id, content_items.target_url AS "
            "target_url, content_items.read_request_id AS read_request_id, content_items.state AS "
            "state, content_items.media_sha256 AS media_sha256, content_items.fingerprint AS "
            "fingerprint, content_items.captured_privacy_epoch AS captured_privacy_epoch, "
            "content_items.reserved_bytes AS reserved_bytes, content_items.usage_request_id AS "
            "usage_request_id, content_items.output_event_id AS output_event_id, "
            "content_items.lease_token AS lease_token, content_items.lease_until AS lease_until, "
            "content_items.deadline AS deadline, content_items.error_code AS error_code, "
            "content_items.redacted AS redacted, content_items.created_at AS created_at, "
            "content_items.started_at AS started_at, content_items.updated_at AS updated_at, "
            "content_items.attachment_json AS attachment_json FROM content_items AS content_items "
            "WHERE content_items.kind = 'qq_file' AND content_items.state = 'QUEUED' AND "
            "content_items.redacted = 0 ORDER BY record_sequence NULLS FIRST LIMIT 1"
        )
        if not row:
            return False
        try:
            _, _, files, _, approval = _guard(
                service, config, row["owner_event_id"], item_id=row["item_id"]
            )
            if row["captured_privacy_epoch"] != epoch(s):
                raise CoreError("PRIVACY_CHANGED", 409)
            if row["deadline"] <= now:
                raise CoreError("PRIVATE_FILE_WAIT_EXPIRED", 410)
            original = _original(files, row)
            snapshot = _snapshot(row, original, config)
            data, mime = image_spool.get(s, row, now, snapshot["max_bytes"])
            if (
                len(data) != snapshot["actual_bytes"]
                or mime != snapshot["mime_type"]
                or hashlib.sha256(data).hexdigest() != row["media_sha256"]
            ):
                raise CoreError("PRIVATE_FILE_ORIGINAL_CHANGED", 409)
            staged = s.one(
                "SELECT value FROM runtime_meta WHERE key = %s", (qq_files.STAGED + row["item_id"],)
            )
            proof = json.loads(staged["value"]) if staged else {}
            qq_files.upload_proof(
                approval,
                row["owner_event_id"],
                original,
                proof.get("lease_token"),
                row["media_sha256"],
                mime,
                len(data),
                proof,
            )
            if (
                digest({"attachment": snapshot, "sha256": row["media_sha256"], "proof": proof})
                != row["fingerprint"]
            ):
                raise CoreError("PRIVATE_FILE_ORIGINAL_CHANGED", 409)
        except (CoreError, ValueError, TypeError, KeyError, IndexError) as error:
            _failed(
                service,
                row,
                error.code if isinstance(error, CoreError) else "PRIVATE_FILE_ORIGINAL_CHANGED",
            )
            return True
        from companion import runtime_config

        runtime_config.bind(s, service.settings, service.clock, "content:" + row["item_id"])
        token = uid("content_lease")
        s.execute(
            "UPDATE content_items SET state = 'RUNNING', lease_token = %s, lease_until = %s, "
            "updated_at = %s WHERE item_id = %s",
            (
                token,
                min(row["deadline"], after(now, config.worker_lease_seconds)),
                now,
                row["item_id"],
            ),
        )
    result, error = None, None
    loop, cancelled = asyncio.get_running_loop(), threading.Event()

    async def check_dispatch():
        # SQLite and authority checks stay on the owning event loop, not parser threads.
        with _transaction(s):
            current = s.one(
                "SELECT content_items.item_id AS item_id, content_items.owner_event_id "
                "AS owner_event_id, content_items.kind AS kind, content_items.slot AS slot, "
                "content_items.source_event_id AS source_event_id, content_items.target_url AS "
                "target_url, content_items.read_request_id AS read_request_id, content_items.state "
                "AS state, content_items.media_sha256 AS media_sha256, content_items.fingerprint "
                "AS fingerprint, content_items.captured_privacy_epoch AS captured_privacy_epoch, "
                "content_items.reserved_bytes AS reserved_bytes, content_items.usage_request_id AS "
                "usage_request_id, content_items.output_event_id AS output_event_id, "
                "content_items.lease_token AS lease_token, content_items.lease_until AS "
                "lease_until, content_items.deadline AS deadline, content_items.error_code AS "
                "error_code, content_items.redacted AS redacted, content_items.created_at AS "
                "created_at, content_items.started_at AS started_at, content_items.updated_at AS "
                "updated_at, content_items.attachment_json AS attachment_json FROM content_items "
                "AS content_items WHERE content_items.item_id = %s",
                (row["item_id"],),
            )
            if (
                cancelled.is_set()
                or not current
                or current["state"] != "RUNNING"
                or current["lease_token"] != token
                or current["redacted"]
                or current["captured_privacy_epoch"] != epoch(s)
                or current["lease_until"] <= service.clock()
                or current["deadline"] <= service.clock()
                or current["attachment_json"] != encode(snapshot)
                or current["fingerprint"] != row["fingerprint"]
                or current["media_sha256"] != row["media_sha256"]
            ):
                raise CoreError("PRIVATE_FILE_LEASE_FENCED", 409)
            _, _, files, _, approval = _guard(
                service, config, row["owner_event_id"], item_id=row["item_id"]
            )
            original = _original(files, current)
            _snapshot(current, original, config)
            stored, stored_mime = image_spool.get(
                s, current, service.clock(), snapshot["max_bytes"]
            )
            staged = s.one(
                "SELECT value FROM runtime_meta WHERE key = %s", (qq_files.STAGED + row["item_id"],)
            )
            if (
                stored != data
                or stored_mime != mime
                or not staged
                or json.loads(staged["value"]) != proof
            ):
                raise CoreError("PRIVATE_FILE_ORIGINAL_CHANGED", 409)
            qq_files.upload_proof(
                approval,
                row["owner_event_id"],
                original,
                proof.get("lease_token"),
                row["media_sha256"],
                mime,
                len(data),
                proof,
            )

    def effect_check():
        if cancelled.is_set():
            raise CoreError("PRIVATE_FILE_PROCESSOR_CANCELLED", 409)
        pending = asyncio.run_coroutine_threadsafe(check_dispatch(), loop)
        try:
            pending.result(timeout=10)
        finally:
            pending.cancel()

    try:
        parser = parse or (
            file_parser_client.parse_via_socket
            if snapshot["parser_socket"]
            else file_parser.parse_isolated
        )
        transport = (
            {"socket_path": snapshot["parser_socket"], "request_id": row["item_id"]}
            if snapshot["parser_socket"]
            else {"workspace": s.state_directory}
        )
        result = await asyncio.to_thread(
            parser,
            data,
            snapshot["filename"],
            mime,
            image=snapshot["parser_image"],
            **transport,
            limits=file_parser.Limits(**snapshot["parser_limits"]),
            effect_check=effect_check,
        )
        file_parser._validate_result(
            {**result, "isolation": "not_asserted"},
            file_parser._result(data, snapshot["filename"], mime),
            file_parser.Limits(**snapshot["parser_limits"]),
        )
    except asyncio.CancelledError:
        cancelled.set()
        with _transaction(s):
            current = s.one(
                "SELECT content_items.item_id AS item_id, content_items.owner_event_id "
                "AS owner_event_id, content_items.kind AS kind, content_items.slot AS slot, "
                "content_items.source_event_id AS source_event_id, content_items.target_url AS "
                "target_url, content_items.read_request_id AS read_request_id, content_items.state "
                "AS state, content_items.media_sha256 AS media_sha256, content_items.fingerprint "
                "AS fingerprint, content_items.captured_privacy_epoch AS captured_privacy_epoch, "
                "content_items.reserved_bytes AS reserved_bytes, content_items.usage_request_id AS "
                "usage_request_id, content_items.output_event_id AS output_event_id, "
                "content_items.lease_token AS lease_token, content_items.lease_until AS "
                "lease_until, content_items.deadline AS deadline, content_items.error_code AS "
                "error_code, content_items.redacted AS redacted, content_items.created_at AS "
                "created_at, content_items.started_at AS started_at, content_items.updated_at AS "
                "updated_at, content_items.attachment_json AS attachment_json FROM content_items "
                "AS content_items WHERE content_items.item_id = %s",
                (row["item_id"],),
            )
            if current and current["lease_token"] == token:
                _failed(service, current, "PRIVATE_FILE_PROCESSOR_CANCELLED", state="UNKNOWN")
        raise
    except Exception:
        error = "PRIVATE_FILE_PROCESSOR_FAILED"
    finally:
        cancelled.set()
    with _transaction(s):
        current = s.one(
            "SELECT content_items.item_id AS item_id, content_items.owner_event_id AS "
            "owner_event_id, content_items.kind AS kind, content_items.slot AS slot, "
            "content_items.source_event_id AS source_event_id, content_items.target_url AS "
            "target_url, content_items.read_request_id AS read_request_id, content_items.state AS "
            "state, content_items.media_sha256 AS media_sha256, content_items.fingerprint AS "
            "fingerprint, content_items.captured_privacy_epoch AS captured_privacy_epoch, "
            "content_items.reserved_bytes AS reserved_bytes, content_items.usage_request_id AS "
            "usage_request_id, content_items.output_event_id AS output_event_id, "
            "content_items.lease_token AS lease_token, content_items.lease_until AS lease_until, "
            "content_items.deadline AS deadline, content_items.error_code AS error_code, "
            "content_items.redacted AS redacted, content_items.created_at AS created_at, "
            "content_items.started_at AS started_at, content_items.updated_at AS updated_at, "
            "content_items.attachment_json AS attachment_json FROM content_items AS content_items "
            "WHERE content_items.item_id = %s",
            (row["item_id"],),
        )
        if (
            not current
            or current["state"] != "RUNNING"
            or current["lease_token"] != token
            or current["redacted"]
        ):
            _discard(service, row["item_id"])
            return True
        try:
            _, _, files, _, _ = _guard(
                service, config, row["owner_event_id"], item_id=row["item_id"]
            )
            _snapshot(current, _original(files, row), config)
            if (
                current["captured_privacy_epoch"] != epoch(s)
                or current["attachment_json"] != encode(snapshot)
                or current["fingerprint"] != row["fingerprint"]
            ):
                raise CoreError("PRIVATE_FILE_SOURCE_CHANGED", 409)
            if current["lease_until"] <= service.clock() or current["deadline"] <= service.clock():
                raise CoreError("PRIVATE_FILE_LEASE_FENCED", 409)
            if error:
                _failed(service, current, error)
            elif result["status"] in {"ERROR", "UNAVAILABLE"}:
                _failed(
                    service,
                    current,
                    result["error_code"],
                    state="UNKNOWN" if result["status"] == "UNAVAILABLE" else "FAILED",
                )
            else:
                _record(service, current, snapshot, result, config)
        except (CoreError, ValueError, TypeError, KeyError) as failed:
            _failed(
                service,
                current,
                failed.code if isinstance(failed, CoreError) else "PRIVATE_FILE_EVIDENCE_INVALID",
            )
        _discard(service, row["item_id"])
    return True
