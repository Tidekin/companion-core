"""Delivery-scoped original byte reads under the existing native Outbox lease."""

import base64
import json

from pydantic import Field

from companion import owner_delivery
from companion.contracts import CoreError
from companion.meme_core import MemeCaptionService
from companion.meme_retrieval import SHA, AssetBinding, MemeRetrieval, core_owned
from companion.owner_authority_protocol import AuthorityReference
from companion_meme_protocol import digest


class AssetRequest(AuthorityReference):
    event_id: str = Field(pattern=r"^evt_[a-f0-9]{64}$")
    reply_revision: int = Field(strict=True, ge=1, lt=2**53)
    lease_token: str = Field(pattern=r"^lease_[a-f0-9]{32}$")
    content_sha256: SHA


def read_asset(store, settings, clock, message_id, body):
    if not core_owned(settings):
        raise CoreError("MEME_CORE_MODE_REQUIRED", 403)
    with store.transaction():
        permission = owner_delivery.check_delivery(store, settings, clock, message_id)
        row, receipt = owner_delivery.asset_lease(store, clock, message_id, body, permission)
        saved = store.one(
            'SELECT value FROM runtime_meta WHERE key = %s', (owner_delivery.DELIVERY + message_id,)
        )
        material = json.loads(saved["value"])
        job = store.one('SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, jobs.kind AS '
            'kind, jobs.state AS state, jobs.idempotency_key AS idempotency_key, jobs.attempts AS '
            'attempts, jobs.max_attempts AS max_attempts, jobs.lease_owner AS lease_owner, '
            'jobs.lease_until AS lease_until, jobs.next_attempt_at AS next_attempt_at, '
            'jobs.last_error_code AS last_error_code, jobs.created_at AS created_at, '
            'jobs.updated_at AS updated_at, jobs.lease_token AS lease_token, jobs.context_tracked '
            'AS context_tracked, jobs.authority_json AS authority_json FROM jobs AS jobs WHERE '
            'jobs.job_id = %s', (material["job_id"],))
        retrieval = MemeRetrieval(MemeCaptionService(store, settings, None, clock))
        raw = retrieval.selected(job, json.loads(row["content_json"]).get("image") or {})
        binding = AssetBinding.model_validate(raw)
        _, _, _, _, image = retrieval._resolve(binding.asset_id, binding.caption_job_id)
        return {
            **receipt,
            **{
                key: raw[key]
                for key in (
                    "asset_id",
                    "asset_sha256",
                    "source_sha256",
                    "custody_sha256",
                    "record_sha256",
                    "caption_sha256",
                )
            },
            "binding_sha256": digest(raw),
            "mime": image.mime_type,
            "byte_length": len(image.data),
            "data_base64": base64.b64encode(image.data).decode(),
        }
