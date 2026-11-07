"""Staged assets and their original evidence; never an execution authority."""

import hashlib
from datetime import datetime
from types import SimpleNamespace
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from companion import owner_input
from companion.contracts import CoreError
from companion.meme_core import _proof
from companion.qq_rich import input_digest
from companion_meme_protocol import CaptionContext, digest, parse, source_evidence, strict_json

INTENTS = {"celebrate", "comfort", "like", "surprised", "morning", "sleep", "playful"}


class Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    category: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    description: str = Field(min_length=1, max_length=2000)
    tags: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(max_length=32)
    suitable: list[str] = Field(max_length=7)
    avoid: list[str] = Field(max_length=7)
    source_page: str = Field(min_length=1, max_length=2048)
    asset_url: str = Field(min_length=1, max_length=2048)
    license: str = Field(min_length=1, max_length=256)
    usage_basis: str = Field(min_length=1, max_length=512)
    fetched_at: str = Field(min_length=1, max_length=64)
    revision: str = Field(max_length=200)
    acquisition_json: str = Field(max_length=32768)


def metadata(value):
    parsed = Metadata.model_validate(value).model_dump()
    if any(v not in INTENTS for v in parsed["suitable"] + parsed["avoid"]) or any(
        not parsed[k].strip()
        for k in ("description", "source_page", "asset_url", "license", "usage_basis", "fetched_at")
    ):
        raise ValueError("MEME_ASSET_METADATA_INVALID")
    return parsed


def source_digest(value):
    return digest(source_evidence(SimpleNamespace(**value)))


def custody_digest(custody):
    return digest({key: custody[key] for key in ("identity", "capture_public_key")})


def asset_id(custody, item):
    return digest([custody_digest(custody), item["source_sha256"], item["asset_sha256"]])


def record_digest(item):
    return digest(
        {
            "asset_sha256": item["asset_sha256"],
            "source_sha256": item["source_sha256"],
            "metadata_sha256": digest(item["metadata"]),
        }
    )


def store_original(store, custody, item, data, sources, withdrawn):
    """Original immutable record/terminal-state rules, inside the caller transaction."""
    from companion_meme_protocol import canonical

    if not store.in_transaction:
        raise RuntimeError("MEME_ASSET_STAGE_REQUIRES_TRANSACTION")
    key, content_hash = asset_id(custody, item), record_digest(item)
    prior = store.one(
        "SELECT meme_assets.asset_id AS asset_id, meme_assets.custody_sha256 AS "
        "custody_sha256, meme_assets.asset_sha256 AS asset_sha256, meme_assets.source_sha256 AS "
        "source_sha256, meme_assets.record_sha256 AS record_sha256, meme_assets.state AS state, "
        "meme_assets.mime AS mime, meme_assets.data AS data, meme_assets.metadata_json AS "
        "metadata_json FROM meme_assets AS meme_assets WHERE meme_assets.asset_id = %s",
        (key,),
    )
    if prior and prior["record_sha256"] != content_hash:
        raise ValueError("MEME_ASSET_CONTENT_CONFLICT")
    state = (
        "ERASED"
        if withdrawn
        else "REMOVED"
        if not item["active"]
        else "STAGED"
        if sources
        else "QUARANTINED"
    )
    if prior:
        if prior["state"] == "ERASED" or prior["state"] == "REMOVED" and state != "ERASED":
            state = prior["state"]
        elif prior["state"] == "QUARANTINED" and state == "STAGED":
            state = "QUARANTINED"
    values = {
        "state": state,
        "data": data if state == "STAGED" else None,
        "metadata_json": canonical(item["metadata"]) if state == "STAGED" else None,
        "mime": item["mime"] if state == "STAGED" else None,
    }
    changed = not prior or any(prior[k] != v for k, v in values.items())
    if not prior:
        store.insert(
            "meme_assets",
            asset_id=key,
            custody_sha256=custody_digest(custody),
            asset_sha256=item["asset_sha256"],
            source_sha256=item["source_sha256"],
            record_sha256=content_hash,
            **values,
        )
    elif changed:
        store.execute(
            "UPDATE meme_assets SET state = %s, data = %s, metadata_json = %s, mime = %s "
            "WHERE asset_id = %s",
            (*values.values(), key),
        )
    for event, job in sources:
        store.execute(
            "INSERT INTO meme_asset_sources VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (key, event, job),
        )
    return {"asset_id": key, "state": state, "changed": changed}


def check_capacity(store, *, max_rows=4096, max_bytes=64 * 1024 * 1024):
    total = store.one(
        "SELECT COUNT(*) AS n, COALESCE(CAST(SUM(LENGTH(data)) AS BIGINT), 0) AS "
        "size FROM meme_assets"
    )
    if total["n"] > max_rows or total["size"] > max_bytes:
        raise ValueError("MEME_ASSET_STORAGE_LIMIT")


def stage_native(store, settings, clock, *, caption_job_id, metadata_value, data, custody):
    from companion import meme_policy, meme_service
    from companion.meme_core import check_caption_job
    from companion.meme_retrieval import media

    if not store.in_transaction:
        raise RuntimeError("MEME_ASSET_STAGE_REQUIRES_TRANSACTION")
    approval = meme_service._native_scope(store, settings, clock)
    job = store.one(
        "SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, jobs.kind AS kind, "
        "jobs.state AS state, jobs.idempotency_key AS idempotency_key, jobs.attempts AS attempts, "
        "jobs.max_attempts AS max_attempts, jobs.lease_owner AS lease_owner, jobs.lease_until AS "
        "lease_until, jobs.next_attempt_at AS next_attempt_at, jobs.last_error_code AS "
        "last_error_code, jobs.created_at AS created_at, jobs.updated_at AS updated_at, "
        "jobs.lease_token AS lease_token, jobs.context_tracked AS context_tracked, "
        "jobs.authority_json AS authority_json FROM jobs AS jobs WHERE jobs.job_id = "
        "%s",
        (caption_job_id,),
    )
    if not job:
        raise CoreError("MEME_CAPTION_JOB_MISSING", 404)
    check_caption_job(store, settings, approval, job, clock=clock)
    caption = store.one(
        "SELECT meme_captions.job_id AS job_id, meme_captions.cache_key AS "
        "cache_key, meme_captions.request_sha256 AS request_sha256, meme_captions.asset_sha256 AS "
        "asset_sha256, meme_captions.source_sha256 AS source_sha256, meme_captions.identity_json "
        "AS identity_json, meme_captions.context_json AS context_json, meme_captions.source_json "
        "AS source_json, meme_captions.request_json AS request_json, meme_captions.asset_data AS "
        "asset_data, meme_captions.result_json AS result_json, meme_captions.usage_request_id AS "
        "usage_request_id, meme_captions.reuse_job_id AS reuse_job_id, meme_captions.plan_sha256 "
        "AS plan_sha256, meme_captions.privacy_epoch AS privacy_epoch, meme_captions.created_at AS "
        "created_at, meme_captions.updated_at AS updated_at FROM meme_captions AS meme_captions "
        "WHERE meme_captions.job_id = %s",
        (caption_job_id,),
    )
    meta, image = metadata(metadata_value), media(settings, data)
    item = {
        "asset_sha256": caption["asset_sha256"],
        "source_sha256": caption["source_sha256"],
        "active": True,
        "metadata": meta,
        "mime": image.mime_type if image else "",
    }
    acquired = strict_json(meta["acquisition_json"])
    if (
        image is None
        or image.sha256 in settings.meme_excluded_sha256
        or image.sha256 != item["asset_sha256"]
        or source_digest(meta) != item["source_sha256"]
        or original_custody(store, settings, job["event_id"]) != custody
        or not ({"source_files", "gallery_selection"} & acquired.keys())
        or acquired.get("archive_member")
        or not meme_policy.source_allowed(meta, settings)
    ):
        raise CoreError("MEME_ASSET_SOURCE_CHANGED", 403)
    sources, withdrawn = provenance(
        store, settings, custody, item, data, caption_job_id=caption_job_id
    )
    if not sources or withdrawn:
        raise CoreError("MEME_ASSET_SOURCE_CHANGED", 403)
    prior = store.one(
        "SELECT meme_assets.asset_id AS asset_id, meme_assets.custody_sha256 AS "
        "custody_sha256, meme_assets.asset_sha256 AS asset_sha256, meme_assets.source_sha256 AS "
        "source_sha256, meme_assets.record_sha256 AS record_sha256, meme_assets.state AS state, "
        "meme_assets.mime AS mime, meme_assets.data AS data, meme_assets.metadata_json AS "
        "metadata_json FROM meme_assets AS meme_assets WHERE meme_assets.asset_id = "
        "%s",
        (asset_id(custody, item),),
    )
    if prior and prior["state"] != "STAGED":
        raise CoreError("MEME_ASSET_UNAVAILABLE", 409)
    result = store_original(store, custody, item, data, sources, withdrawn)
    check_capacity(store)
    if result["state"] != "STAGED":
        raise CoreError("MEME_ASSET_UNAVAILABLE", 409)
    return result["asset_id"], 0 if prior else len(data)


def erased(store, event_id):
    event = store.one("SELECT redacted FROM events WHERE event_id = %s", (event_id,))
    return bool(
        not event
        or event["redacted"]
        or store.one("SELECT 1 FROM runtime_meta WHERE key = %s", ("erased_event:" + event_id,))
    )


def _capture(store, settings, event_id, custody, authority):
    event = store.one(
        "SELECT e.event_id AS event_id, e.schema_version AS schema_version, e.source AS source, "
        "e.source_event_id AS source_event_id, e.conversation_id AS conversation_id, e.actor_id AS "
        "actor_id, e.event_type AS event_type, e.occurred_at AS occurred_at, e.ingested_at AS "
        "ingested_at, e.privacy_scope AS privacy_scope, e.body_json AS body_json, e.trace_id AS "
        "trace_id, e.idempotency_key AS idempotency_key, e.fingerprint AS fingerprint, e.redacted "
        "AS redacted, e.memory_processed AS memory_processed, e.reply_revision AS reply_revision, "
        "c.scope AS scope, c.owner_actor_id AS owner_actor_id, c.platform_session_key AS "
        "platform_session_key FROM events AS e JOIN conversations AS c ON e.conversation_id = "
        "c.conversation_id WHERE e.event_id = %s",
        (event_id,),
    )
    saved = store.one("SELECT value FROM runtime_meta WHERE key = %s", ("owner_input:" + event_id,))
    value = strict_json(saved["value"]) if saved else {}
    if not store.one("SELECT 1 FROM runtime_meta WHERE key = 'model_budget_journal_id'"):
        raise ValueError("MEME_ASSET_SOURCE_UNATTRIBUTED")
    capture, view = owner_input.verify(store, settings, value.get("capture", {}))
    identity = custody["identity"]
    if (
        not event
        or event["redacted"]
        or event["source"] != "qq.onebot"
        or event["actor_id"] != "owner"
        or event["scope"] != "private"
        or event["owner_actor_id"] != "owner"
        or event["privacy_scope"] != "private"
        or event["event_type"] != "message.received"
        or capture.event_id != event_id
        or capture.source_event_id != event["source_event_id"]
        or capture.occurred_at != datetime.fromisoformat(event["occurred_at"])
        or capture.input_sha256 != input_digest(strict_json(event["body_json"]))
        or value.get("input_sha256") != capture.input_sha256
        or value.get("authority") != capture.authority
        or capture.authority != authority
        or identity
        != [
            settings.owner_qq_id,
            settings.bot_qq_id,
            event["platform_session_key"],
            capture.protocol_epoch,
        ]
        or custody["capture_public_key"] != view.policy.capture.public_key
    ):
        raise ValueError("MEME_ASSET_SOURCE_UNATTRIBUTED")


def original_custody(store, settings, event_id):
    """Derive custody from the original signed capture, never a new grant."""
    event = store.one(
        "SELECT platform_session_key FROM events JOIN conversations USING "
        "(conversation_id) WHERE event_id = %s",
        (event_id,),
    )
    saved = store.one("SELECT value FROM runtime_meta WHERE key = %s", ("owner_input:" + event_id,))
    if not event or not saved:
        raise ValueError("MEME_ASSET_SOURCE_UNATTRIBUTED")
    value = strict_json(saved["value"])
    capture, view = owner_input.verify(store, settings, value.get("capture", {}))
    custody = {
        "identity": [
            settings.owner_qq_id,
            settings.bot_qq_id,
            event["platform_session_key"],
            capture.protocol_epoch,
        ],
        "capture_public_key": view.policy.capture.public_key,
    }
    _capture(store, settings, event_id, custody, capture.authority)
    return custody


def extend_reuse_provenance(store, settings, affected_caption_job_ids):
    """Maintain complete attribution in the caller's original repoint transaction."""
    from companion.store import encode

    if not store.in_transaction:
        raise RuntimeError("MEME_ASSET_REPOINT_REQUIRES_TRANSACTION")
    assets = store.rows(
        "SELECT DISTINCT a.asset_id AS asset_id, a.custody_sha256 AS custody_sha256, "
        "a.asset_sha256 AS asset_sha256, a.source_sha256 AS source_sha256, a.record_sha256 AS "
        "record_sha256, a.state AS state, a.mime AS mime, a.data AS data, a.metadata_json AS "
        "metadata_json FROM meme_assets AS a JOIN meme_asset_sources AS src ON a.asset_id = "
        "src.asset_id WHERE a.state = 'STAGED' AND src.caption_job_id IN (SELECT parsed.value "
        'COLLATE "C" AS _col_0 FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS parsed(value))',
        (encode(sorted(affected_caption_job_ids)),),
    )
    try:
        for asset in assets:
            meta = metadata(strict_json(asset["metadata_json"]))
            item = {
                "asset_sha256": asset["asset_sha256"],
                "source_sha256": asset["source_sha256"],
                "active": True,
                "metadata": meta,
            }
            if (
                not asset["data"]
                or hashlib.sha256(asset["data"]).hexdigest() != asset["asset_sha256"]
                or source_digest(meta) != asset["source_sha256"]
                or digest(
                    {
                        "asset_sha256": asset["asset_sha256"],
                        "source_sha256": asset["source_sha256"],
                        "metadata_sha256": digest(meta),
                    }
                )
                != asset["record_sha256"]
            ):
                raise ValueError("MEME_ASSET_SOURCE_UNATTRIBUTED")
            anchors = store.rows(
                "SELECT DISTINCT c.job_id, j.event_id FROM meme_asset_sources AS src JOIN "
                "meme_captions AS c ON c.job_id = src.caption_job_id JOIN jobs AS j USING (job_id) "
                "WHERE src.asset_id = %s AND c.asset_sha256 = %s AND c.source_sha256 = %s",
                (asset["asset_id"], asset["asset_sha256"], asset["source_sha256"]),
            )
            if not anchors:
                raise ValueError("MEME_ASSET_SOURCE_UNATTRIBUTED")
            for anchor in anchors:
                custody = original_custody(store, settings, anchor["event_id"])
                if (
                    custody_digest(custody) != asset["custody_sha256"]
                    or asset_id(custody, item) != asset["asset_id"]
                ):
                    raise ValueError("MEME_ASSET_SOURCE_UNATTRIBUTED")
                sources, withdrawn = provenance(
                    store, settings, custody, item, asset["data"], caption_job_id=anchor["job_id"]
                )
                if not sources or withdrawn:
                    raise ValueError("MEME_ASSET_SOURCE_UNATTRIBUTED")
                # Old ancestors remain conservative erasure attribution. Tuple identity
                # includes caption_job_id even when every ancestor shares one event.
                for event, job in sources:
                    store.execute(
                        "INSERT INTO meme_asset_sources VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (asset["asset_id"], event, job),
                    )
    except (CoreError, ValueError, KeyError, TypeError) as error:
        raise CoreError("MEME_ASSET_SOURCE_CHANGED", 403) from error


def provenance(store, settings, custody, item, data, *, caption_job_id=None):
    """Resolve originals independently of manifest hints, including reuse ancestors.

    A known erased original yields only a tombstone. Unverifiable legacy material
    yields no content or fabricated attribution; neither path grants execution.
    """
    candidates = store.rows(
        (
            "SELECT c.*,j.event_id,j.authority_json FROM "
            "meme_captions c JOIN jobs j USING(job_id) WHERE "
            "c.asset_sha256=%s AND c.source_sha256=%s AND "
            "j.kind='meme_caption' "
        )
        + ("AND c.job_id=%s " if caption_job_id else "")
        + 'ORDER BY c.record_sequence',
        (
            item["asset_sha256"],
            item["source_sha256"],
            *((caption_job_id,) if caption_job_id else ()),
        ),
    )
    proof = source_evidence(SimpleNamespace(**item["metadata"]))
    for candidate in candidates:
        lineage, pending, seen = [], [candidate], set()
        while pending:
            row = pending.pop()
            if row["job_id"] in seen:
                raise ValueError("MEME_ASSET_LINEAGE_INVALID")
            seen.add(row["job_id"])
            lineage.append(row)
            if row["reuse_job_id"]:
                parent = store.one(
                    "SELECT c.job_id AS job_id, c.cache_key AS cache_key, c.request_sha256 AS "
                    "request_sha256, c.asset_sha256 AS asset_sha256, c.source_sha256 AS "
                    "source_sha256, c.identity_json AS identity_json, c.context_json AS "
                    "context_json, c.source_json AS source_json, c.request_json AS request_json, "
                    "c.asset_data AS asset_data, c.result_json AS result_json, c.usage_request_id "
                    "AS usage_request_id, c.reuse_job_id AS reuse_job_id, c.plan_sha256 AS "
                    "plan_sha256, c.privacy_epoch AS privacy_epoch, c.created_at AS created_at, "
                    "c.updated_at AS updated_at, j.event_id AS event_id, j.authority_json AS "
                    "authority_json FROM meme_captions AS c JOIN jobs AS j ON c.job_id = j.job_id "
                    "WHERE c.job_id = %s AND j.kind = 'meme_caption'",
                    (row["reuse_job_id"],),
                )
                if parent is None:
                    raise ValueError("MEME_ASSET_LINEAGE_INVALID")
                pending.append(parent)
        sources = {(r["event_id"], r["job_id"]) for r in lineage}
        for row in lineage:
            sources.update(
                (s["event_id"], row["job_id"])
                for s in store.rows(
                    "SELECT event_id FROM job_sources WHERE job_id = %s",
                    (row["job_id"],),
                )
            )
        if any(erased(store, event) for event, _ in sources):
            return sorted(sources), True
        try:
            if (
                strict_json(candidate["source_json"]) != proof
                or item["active"]
                and candidate["asset_data"] != data
            ):
                continue
            _proof(proof, item["asset_sha256"], item["source_sha256"])
            for row in lineage:
                authority = strict_json(row["authority_json"])
                if not store.one(
                    "SELECT 1 FROM job_sources WHERE job_id = %s AND event_id = %s",
                    (row["job_id"], row["event_id"]),
                ):
                    raise ValueError("MEME_ASSET_SOURCE_UNATTRIBUTED")
                _capture(store, settings, row["event_id"], custody, authority)
                context = parse(CaptionContext, row["context_json"])
                parent = store.one(
                    "SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, "
                    "jobs.kind AS kind, jobs.state AS state, jobs.idempotency_key AS "
                    "idempotency_key, jobs.attempts AS attempts, jobs.max_attempts AS "
                    "max_attempts, jobs.lease_owner AS lease_owner, jobs.lease_until AS "
                    "lease_until, jobs.next_attempt_at AS next_attempt_at, jobs.last_error_code AS "
                    "last_error_code, jobs.created_at AS created_at, jobs.updated_at AS "
                    "updated_at, jobs.lease_token AS lease_token, jobs.context_tracked AS "
                    "context_tracked, jobs.authority_json AS authority_json FROM jobs AS jobs "
                    "WHERE jobs.job_id = %s AND jobs.kind = 'meme_discovery'",
                    (context.discovery_job_id,),
                )
                reservation = store.one(
                    "SELECT meme_discovery_reservations.job_id AS job_id, "
                    "meme_discovery_reservations.attempt_key AS attempt_key, "
                    "meme_discovery_reservations.day AS day, meme_discovery_reservations.state AS "
                    "state, meme_discovery_reservations.reserved_queries AS reserved_queries, "
                    "meme_discovery_reservations.queries AS queries, "
                    "meme_discovery_reservations.reserved_requests AS reserved_requests, "
                    "meme_discovery_reservations.requests AS requests, "
                    "meme_discovery_reservations.reserved_download_bytes AS "
                    "reserved_download_bytes, meme_discovery_reservations.download_bytes AS "
                    "download_bytes, meme_discovery_reservations.reserved_storage_bytes AS "
                    "reserved_storage_bytes, meme_discovery_reservations.storage_bytes AS "
                    "storage_bytes, meme_discovery_reservations.reserved_model_cost AS "
                    "reserved_model_cost, meme_discovery_reservations.model_cost AS model_cost, "
                    "meme_discovery_reservations.created_at AS created_at, "
                    "meme_discovery_reservations.updated_at AS updated_at FROM "
                    "meme_discovery_reservations AS meme_discovery_reservations WHERE "
                    "meme_discovery_reservations.job_id = %s",
                    (context.discovery_job_id,),
                )
                source = store.one(
                    "SELECT value FROM runtime_meta WHERE key = %s",
                    ("meme_discovery:" + context.discovery_job_id,),
                )
                source = strict_json(source["value"]) if source else {}
                if (
                    not parent
                    or not reservation
                    or reservation["state"] != "SETTLED"
                    or parent["state"] != "SUCCEEDED"
                    or parent["event_id"] != row["event_id"]
                    or strict_json(parent["authority_json"]) != authority
                    or reservation["attempt_key"] != context.attempt_key
                    or source.get("event_id") != row["event_id"]
                    or source.get("reply_revision") != context.reply_revision
                    or context.event_id != row["event_id"]
                    or row["asset_sha256"] != item["asset_sha256"]
                    or not row["asset_data"]
                    or hashlib.sha256(row["asset_data"]).hexdigest() != row["asset_sha256"]
                ):
                    raise ValueError("MEME_ASSET_SOURCE_UNATTRIBUTED")
                _proof(strict_json(row["source_json"]), row["asset_sha256"], row["source_sha256"])
            for event, job in sources:
                authority = next(r["authority_json"] for r in lineage if r["job_id"] == job)
                _capture(store, settings, event, custody, strict_json(authority))
            return sorted(sources), False
        except (CoreError, ValueError, KeyError, TypeError):
            continue
    return [], False


def erase(store, event_ids):
    """Idempotent projection cleanup in the original Ledger transaction."""
    if not store.in_transaction:
        raise RuntimeError("ERASURE_REQUIRES_TRANSACTION")
    if not event_ids:
        return
    from companion.store import encode

    events = encode(sorted(event_ids))
    store.execute(
        "DELETE FROM meme_vectors WHERE asset_id IN (SELECT asset_id FROM meme_asset_sources WHERE "
        'event_id IN (SELECT value COLLATE "C" FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS '
        "parsed(value))) OR caption_job_id IN (SELECT job_id FROM job_sources WHERE event_id IN "
        '(SELECT value COLLATE "C" FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS '
        "parsed(value))) OR caption_job_id IN (SELECT job_id FROM jobs WHERE event_id IN (SELECT "
        'value COLLATE "C" FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS '
        "parsed(value)))",
        (events, events, events),
    )
    store.execute(
        "UPDATE meme_assets SET state = 'ERASED', data = NULL, metadata_json = NULL, mime = NULL "
        "WHERE asset_id IN (SELECT asset_id FROM meme_asset_sources WHERE event_id IN (SELECT "
        'value COLLATE "C" FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS '
        "parsed(value)))",
        (events,),
    )
