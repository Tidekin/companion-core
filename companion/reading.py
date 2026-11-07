"""Persist Reader authority separately from content, and commit evidence atomically."""

import hashlib
import json
from datetime import UTC

from companion import owner_authority, owner_input, reply_revision
from companion.contracts import CoreError
from companion.exploration import ReadItem, ReadResult
from companion.observation import ArticleEvidence, ReadingEvidence
from companion.privacy import epoch
from companion.reading_protocol import ReadingContext
from companion.source_grants import ReaderReadValidation
from companion.sources import content_target
from companion.store import after, encode, utcnow
from companion.transport import digest

FOLLOWUP = "reading_followup:"


def service_for_store(store, settings, clock):
    """Compose native receipt handling on one caller-owned connection, without startup I/O."""
    from companion.bili_video import BiliVideoService
    from companion.content import register_link_images
    from companion.exploration import ExplorationService
    from companion.goals import GoalEngine
    from companion.observation import ObservationService
    from companion.privacy import ErasureLedger
    from companion.social import SocialPlanner

    observations = ObservationService(store, settings, clock)
    goals = GoalEngine(store, settings, ErasureLedger(settings.erasure_ledger), clock)
    social = SocialPlanner(store, settings, clock)
    exploration = ExplorationService(
        store,
        settings,
        observations,
        goals,
        social,
        lambda owner, event, urls: register_link_images(
            store,
            settings,
            clock,
            settings.content,
            owner,
            event,
            urls,
        ),
        clock,
    )
    reading = ReadingService(store, settings, exploration, clock)
    reading.video = BiliVideoService(store, settings, reading, clock=clock)
    return reading


def followup_item(store, settings, event_id, *, clock=utcnow):
    """Project a source-bound SDK tool selection through the existing Content view."""
    if settings.owner_runtime.mode != "ENABLED" or not store.one(
        "SELECT 1 FROM pg_catalog.pg_class WHERE relnamespace=current_schema()::regnamespace AND "
        "relname='agent_tool_calls' AND relkind IN ('r','p')"
    ):
        return None
    item = store.one(
        "SELECT c.item_id AS item_id, c.owner_event_id AS owner_event_id, c.kind AS kind, c.slot "
        "AS slot, c.source_event_id AS source_event_id, c.target_url AS target_url, "
        "c.read_request_id AS read_request_id, c.state AS state, c.media_sha256 AS media_sha256, "
        "c.fingerprint AS fingerprint, c.captured_privacy_epoch AS captured_privacy_epoch, "
        "c.reserved_bytes AS reserved_bytes, c.usage_request_id AS usage_request_id, "
        "c.output_event_id AS output_event_id, c.lease_token AS lease_token, c.lease_until AS "
        "lease_until, c.deadline AS deadline, c.error_code AS error_code, c.redacted AS redacted, "
        "c.created_at AS created_at, c.started_at AS started_at, c.updated_at AS updated_at, "
        "c.attachment_json AS attachment_json FROM content_items AS c JOIN exploration_requests AS "
        "r ON r.request_id = c.read_request_id JOIN agent_tool_calls AS a ON a.request_id = "
        "r.request_id JOIN jobs AS j ON j.job_id = a.job_id JOIN agent_conversations AS ac ON "
        "ac.job_id = j.job_id WHERE j.event_id = %s AND ac.phase <> 'STOPPED' AND a.state IN "
        "('DEFERRED', 'RETURNED') AND c.kind = 'link' AND c.redacted = 0 AND r.redacted = 0 AND "
        "r.operation = 'read' AND r.goal_step_json = '{}' ORDER BY a.record_sequence DESC NULLS "
        "LAST LIMIT 1",
        (event_id,),
    )
    if not item:
        return None
    try:
        job = store.one(
            "SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, jobs.kind AS "
            "kind, jobs.state AS state, jobs.idempotency_key AS idempotency_key, jobs.attempts AS "
            "attempts, jobs.max_attempts AS max_attempts, jobs.lease_owner AS lease_owner, "
            "jobs.lease_until AS lease_until, jobs.next_attempt_at AS next_attempt_at, "
            "jobs.last_error_code AS last_error_code, jobs.created_at AS created_at, "
            "jobs.updated_at AS updated_at, jobs.lease_token AS lease_token, jobs.context_tracked "
            "AS context_tracked, jobs.authority_json AS authority_json FROM jobs AS jobs WHERE "
            "jobs.event_id = %s AND jobs.kind = 'chat'",
            (event_id,),
        )
        view = owner_authority.check_job(store, settings, clock, job, purpose="content")
        reply_revision.check_job(store, job)
        consumer, _, _ = owner_input.source(store, settings, view, event_id, clock())
        source, _, _ = owner_input.source(
            store,
            settings,
            view,
            item["owner_event_id"],
            clock(),
            history=item["owner_event_id"] != event_id,
        )
        if source["conversation_id"] != consumer[
            "conversation_id"
        ] or not reply_revision.source_active(store, source["event_id"]):
            return None
    except CoreError:
        return None
    return item


def erase_reading(store, event_ids):
    if not store.one(
        "SELECT 1 FROM pg_catalog.pg_class WHERE "
        "relnamespace=current_schema()::regnamespace AND relname='reading_attempts' AND relkind IN "
        "('r','p')"
    ):
        return
    clear = (
        "UPDATE reading_attempts SET state=CASE WHEN state='RUNNING' THEN 'UNKNOWN' "
        "ELSE state END,context_json='{}',allowed_hosts_json='[]',original_url_sha256='',"
        "source_grant_json='null',result_event_ids_json='[]' "
    )
    # A forgotten grant may own only an in-flight read, with no observation/event roots yet.
    # Consult its durable tombstone on both initial erasure and old-backup replay.
    store.execute(
        clear + "WHERE request_id IN (SELECT request_id FROM exploration_requests x "
        "WHERE EXISTS (SELECT 1 FROM runtime_meta "
        "WHERE key='erased_source_grant:' || x.source_grant_id))"
    )
    for event_id in event_ids:
        store.execute(
            (
                (
                    'DELETE FROM runtime_meta WHERE key=%s OR (key LIKE %s '
                    'AND (CASE WHEN value IS JSON THEN '
                    "(value::json#>>'{item_id}') END) IN (SELECT item_id "
                    'FROM content_items WHERE owner_event_id=%s))'
                )
            ),
            (FOLLOWUP + event_id, FOLLOWUP + "%", event_id),
        )
        # Retain only the attempt identity/counters: a late receipt cannot revive content.
        store.execute(
            clear
            + (

                    'WHERE request_id IN (SELECT request_id FROM '
                    'exploration_evidence WHERE event_id=%s) OR EXISTS '
                    '(SELECT 1 FROM json_array_elements_text(context_json::j'
                    "son->'source_event_ids') AS ids(value) WHERE value=%s)"

            ),
            (event_id, event_id),
        )


class ReadingService:
    def __init__(self, store, settings, exploration, clock=utcnow):
        self.store, self.settings, self.exploration, self.clock = (
            store,
            settings,
            exploration,
            clock,
        )

    def _context(self, row, source):
        sources = [
            item["event_id"]
            for item in self.store.rows(
                "SELECT event_id FROM exploration_evidence WHERE request_id = %s ORDER BY event_id "
                "NULLS FIRST",
                (row["request_id"],),
            )
        ]
        step = json.loads(row["goal_step_json"])
        root = step.get("goal_id", row["request_id"])
        if not step:
            job = self.store.one(
                "SELECT job_id FROM agent_tool_calls WHERE request_id = %s ORDER "
                "BY record_sequence NULLS FIRST LIMIT 1",
                (row["request_id"],),
            )
            if job is None:
                job = self.store.one(
                    "SELECT job_id FROM jobs WHERE kind = 'chat' AND event_id IN (SELECT event_id "
                    "FROM exploration_evidence WHERE request_id = %s) ORDER BY record_sequence "
                    "NULLS FIRST",
                    (row["request_id"],),
                )
            if job:
                root = job["job_id"]
        return ReadingContext(
            root_task_id=root,
            source=source,
            source_event_ids=sources,
            privacy_epoch=row["captured_privacy_epoch"],
        )

    def _sources(self, row, context):
        self.exploration.goals.validate_sources(context.source_event_ids)
        for source_id in context.source_event_ids:
            if not reply_revision.source_active(self.store, source_id):
                raise CoreError("REPLY_SOURCE_RECALLED", 409)
            captured_owner = self.store.one(
                "SELECT 1 FROM runtime_meta WHERE key = %s", (owner_input.INPUT + source_id,)
            )
            if row["goal_step_json"] == "{}" and (
                captured_owner or reply_revision.owner_input(self.store, source_id)
            ):
                if self.settings.owner_runtime.mode == "ENABLED":
                    view = owner_authority.current(
                        self.store, self.settings, self.clock, purpose="content"
                    )
                    owner_input.source(self.store, self.settings, view, source_id, self.clock())
                else:
                    reply_revision.require_current(self.store, source_id)

    def lease(self, adapter_id, source_grant=None, *, capabilities=()):
        s = self.store
        with s.transaction():
            policy = self.exploration.adapter(adapter_id)

            def request_limits(row):
                if policy.source == "qzone":
                    from companion.qzone import TARGET_TTL_SECONDS, validate_target

                    captured = self.exploration.observations.grants.captured(row)
                    if captured is None or captured != source_grant:
                        return False
                    validate_target(s, self.settings, self.clock, row)
                    return {
                        "max_items": 1,
                        "max_requests": 1,
                        "deadline": min(
                            after(self.clock(), 30),
                            after(row["created_at"], TARGET_TTL_SECONDS),
                            captured.snapshot()["expires_at"],
                        ),
                    }
                video = getattr(self, "video", None)
                exists = s.one(
                    "SELECT 1 FROM pg_catalog.pg_class WHERE "
                    "relnamespace=current_schema()::regnamespace AND relname='bili_video_items' "
                    "AND relkind IN ('r','p')"
                )
                if not exists or not s.one(
                    "SELECT 1 FROM bili_video_items WHERE request_id = %s", (row["request_id"],)
                ):
                    return None
                if video is None or "bounded_video_media_v1" not in capabilities:
                    return False
                return {**video.lease_limits(row["request_id"], source_grant), "max_items": 1}

            item = self.exploration.lease(
                adapter_id,
                source_grant,
                reading=True,
                source_check=lambda row: self._sources(row, self._context(row, policy.source)),
                request_limits=request_limits,
            )
            if item is None:
                return None
            row = s.one(
                "SELECT exploration_requests.request_id AS request_id, "
                "exploration_requests.request_key AS request_key, exploration_requests.adapter_id "
                "AS adapter_id, exploration_requests.operation AS operation, "
                "exploration_requests.url AS url, exploration_requests.goal_step_json AS "
                "goal_step_json, exploration_requests.state AS state, "
                "exploration_requests.lease_token AS lease_token, exploration_requests.lease_until "
                "AS lease_until, exploration_requests.captured_privacy_epoch AS "
                "captured_privacy_epoch, exploration_requests.reserved_bytes AS reserved_bytes, "
                "exploration_requests.used_bytes AS used_bytes, exploration_requests.result_sha256 "
                "AS result_sha256, exploration_requests.last_error_code AS last_error_code, "
                "exploration_requests.created_at AS created_at, exploration_requests.started_at AS "
                "started_at, exploration_requests.updated_at AS updated_at, "
                "exploration_requests.redacted AS redacted, exploration_requests.reserved_requests "
                "AS reserved_requests, exploration_requests.used_requests AS used_requests, "
                "exploration_requests.source_grant_id AS source_grant_id, "
                "exploration_requests.source_grant_epoch AS source_grant_epoch, "
                "exploration_requests.authority_json AS authority_json, exploration_requests.query "
                "AS query, exploration_requests.discovery_mode AS discovery_mode FROM "
                "exploration_requests AS exploration_requests WHERE "
                "exploration_requests.request_id = %s",
                (item["request_id"],),
            )
            context = self._context(row, policy.source)
            self._sources(row, context)
            hosts = list(self.settings.web_reading_hosts) if policy.source == "web" else []
            if row["operation"] == "read":
                source, canonical = content_target(row["url"], web_hosts=hosts)
                if source != policy.source or canonical != row["url"]:
                    raise CoreError("READER_TARGET_MISMATCH", 400)
            elif policy.source == "web":
                raise CoreError("READER_OPERATION_UNAVAILABLE", 403)
            s.insert(
                "reading_attempts",
                request_id=row["request_id"],
                adapter_id=adapter_id,
                lease_token=item["lease_token"],
                lease_until=row["lease_until"],
                context_json=encode(context.model_dump()),
                allowed_hosts_json=encode(hosts),
                original_url_sha256=hashlib.sha256(row["url"].encode()).hexdigest(),
                source_grant_json=encode(item.get("source_grant")),
                state="RUNNING",
                created_at=self.clock(),
                updated_at=self.clock(),
            )
            result = {
                **item,
                "schema_version": 1,
                "lease_until": row["lease_until"],
                "context": context.model_dump(),
                "allowed_hosts": hosts,
            }
            video = getattr(self, "video", None)
            return video.bind_lease(result) if video is not None else result

    def _attempt(self, adapter_id, request_id, token):
        attempt = self.store.one(
            "SELECT reading_attempts.request_id AS request_id, reading_attempts.adapter_id AS "
            "adapter_id, reading_attempts.lease_token AS lease_token, reading_attempts.lease_until "
            "AS lease_until, reading_attempts.context_json AS context_json, "
            "reading_attempts.allowed_hosts_json AS allowed_hosts_json, "
            "reading_attempts.original_url_sha256 AS original_url_sha256, "
            "reading_attempts.source_grant_json AS source_grant_json, reading_attempts.state AS "
            "state, reading_attempts.result_sha256 AS result_sha256, "
            "reading_attempts.result_event_ids_json AS result_event_ids_json, "
            "reading_attempts.observed_requests AS observed_requests, "
            "reading_attempts.observed_bytes AS observed_bytes, reading_attempts.created_at AS "
            "created_at, reading_attempts.updated_at AS updated_at FROM reading_attempts AS "
            "reading_attempts WHERE reading_attempts.request_id = %s AND "
            "reading_attempts.adapter_id = %s",
            (request_id, adapter_id),
        )
        if not attempt or attempt["lease_token"] != token:
            raise CoreError("STALE_READER_LEASE", 409)
        return attempt

    def _validate(self, attempt, validation, *, current_request=None):
        s = self.store
        if attempt["state"] != "RUNNING" or attempt["lease_until"] <= self.clock():
            raise CoreError("STALE_READER_LEASE", 409)
        if attempt["context_json"] == "{}":
            raise CoreError("PRIVACY_CHANGED", 409)
        context = ReadingContext.model_validate_json(attempt["context_json"])
        if context.privacy_epoch != epoch(s):
            raise CoreError("PRIVACY_CHANGED", 409)
        self.exploration.validate_read(
            attempt["adapter_id"],
            attempt["request_id"],
            validation,
            reading=True,
            current_request=current_request,
        )
        row = s.one(
            "SELECT exploration_requests.request_id AS request_id, "
            "exploration_requests.request_key AS request_key, exploration_requests.adapter_id AS "
            "adapter_id, exploration_requests.operation AS operation, exploration_requests.url AS "
            "url, exploration_requests.goal_step_json AS goal_step_json, "
            "exploration_requests.state AS state, exploration_requests.lease_token AS lease_token, "
            "exploration_requests.lease_until AS lease_until, "
            "exploration_requests.captured_privacy_epoch AS captured_privacy_epoch, "
            "exploration_requests.reserved_bytes AS reserved_bytes, "
            "exploration_requests.used_bytes AS used_bytes, exploration_requests.result_sha256 AS "
            "result_sha256, exploration_requests.last_error_code AS last_error_code, "
            "exploration_requests.created_at AS created_at, exploration_requests.started_at AS "
            "started_at, exploration_requests.updated_at AS updated_at, "
            "exploration_requests.redacted AS redacted, exploration_requests.reserved_requests AS "
            "reserved_requests, exploration_requests.used_requests AS used_requests, "
            "exploration_requests.source_grant_id AS source_grant_id, "
            "exploration_requests.source_grant_epoch AS source_grant_epoch, "
            "exploration_requests.authority_json AS authority_json, exploration_requests.query AS "
            "query, exploration_requests.discovery_mode AS discovery_mode FROM "
            "exploration_requests AS exploration_requests WHERE exploration_requests.request_id = "
            "%s",
            (attempt["request_id"],),
        )
        if hashlib.sha256(row["url"].encode()).hexdigest() != attempt["original_url_sha256"]:
            raise CoreError("READER_TARGET_MISMATCH", 409)
        self._sources(row, context)
        paused = s.one("SELECT value FROM runtime_meta WHERE key = 'life_paused'")
        if (
            paused
            and paused["value"] == "1"
            and self.exploration.autonomous(
                row,
                json.loads(row["goal_step_json"]),
            )
        ):
            raise CoreError("AUTONOMY_PAUSED", 409)
        policy = self.exploration.adapter(attempt["adapter_id"])
        if policy.source != context.source or (
            policy.source == "web"
            and not set(json.loads(attempt["allowed_hosts_json"]))
            <= set(self.settings.web_reading_hosts)
        ):
            raise CoreError("READER_SCOPE_CHANGED", 409)
        return row, context

    def validate_read(self, adapter_id, request_id, validation):
        with self.store.transaction():
            attempt = self._attempt(adapter_id, request_id, validation.lease_token)
            self._validate(attempt, validation)
        return {"valid": True}

    def _evidence(self, attempt, row, context, result):
        if result.status == "SUCCESS" and (not result.used_requests or not result.used_bytes):
            raise CoreError("READER_USAGE_REQUIRED", 400)
        if result.source_grant is not None:
            grant = result.source_grant.snapshot()
        else:
            grant = None
        if grant != json.loads(attempt["source_grant_json"]):
            raise CoreError("SOURCE_GRANT_REVISION_CHANGED", 409)
        if (
            result.used_requests is not None
            and result.used_requests > row["reserved_requests"]
            or result.used_bytes is not None
            and result.used_bytes > row["reserved_bytes"]
        ):
            raise CoreError("READER_USAGE_LIMIT", 400)
        if len(result.observations) > min(40, self.settings.exploration_daily_candidate_limit):
            raise CoreError("READER_ITEM_LIMIT", 400)
        for observation in result.observations:
            provenance = observation.provenance.model_dump(exclude={"source_grant"})
            if provenance != context.model_dump():
                raise CoreError("READING_PROVENANCE_MISMATCH", 400)
            if (
                row["operation"] == "read"
                and hashlib.sha256(
                    observation.original_url.encode(),
                ).hexdigest()
                != attempt["original_url_sha256"]
            ):
                raise CoreError("READER_TARGET_MISMATCH", 400)
            for original, url in ((True, observation.original_url), (False, observation.url)):
                source, canonical = content_target(
                    url,
                    web_hosts=json.loads(attempt["allowed_hosts_json"]),
                )
                if (
                    source != context.source
                    or canonical != url
                    or (
                        row["operation"] == "read"
                        and canonical != row["url"]
                        and (original or context.source != "web")
                    )
                ):
                    raise CoreError("READER_TARGET_MISMATCH", 400)
            fetched = observation.fetched_at.astimezone(UTC).isoformat(timespec="microseconds")
            if not after(row["started_at"], -5) <= fetched <= after(self.clock(), 5):
                raise CoreError("READING_TIME_OUT_OF_RANGE", 400)
            discovered = row["operation"] == "discover"
            if (observation.result_status == "DISCOVERED") != discovered:
                raise CoreError("READING_OPERATION_MISMATCH", 400)
            evidence = observation.evidence
            if discovered and getattr(evidence, "discovery_mode", None) != (
                "recommend" if row["discovery_mode"] == "recommend" else None
            ):
                raise CoreError("READING_DISCOVERY_MISMATCH", 400)
            evidence_bytes = (
                sum(item.bytes for item in evidence.sources)
                if isinstance(evidence, ReadingEvidence)
                else getattr(evidence, "response_bytes", 0)
            )
            if result.used_bytes is None or evidence_bytes > result.used_bytes:
                raise CoreError("READING_EVIDENCE_USAGE_MISMATCH", 400)

    def complete(self, adapter_id, request_id, result):
        s, fingerprint, step = self.store, digest(result.model_dump(mode="json")), None
        with s.transaction():
            if s.one(
                "SELECT 1 FROM pg_catalog.pg_class WHERE "
                "relnamespace=current_schema()::regnamespace AND relname='bili_video_items' AND "
                "relkind IN ('r','p')"
            ) and s.one(
                "SELECT 1 FROM bili_video_items WHERE request_id = %s",
                (request_id,),
            ):
                raise CoreError("READER_MEDIA_PROTOCOL_REQUIRED", 409)
            attempt = self._attempt(adapter_id, request_id, result.lease_token)
            if attempt["result_sha256"]:
                if attempt["result_sha256"] != fingerprint:
                    raise CoreError("IDEMPOTENCY_CONFLICT", 409)
                return {
                    "state": attempt["state"],
                    "duplicate": True,
                    "event_ids": json.loads(attempt["result_event_ids_json"]),
                }
            validation = ReaderReadValidation(
                lease_token=result.lease_token,
                source_grant=result.source_grant,
            )
            try:
                row, context = self._validate(attempt, validation)
            except CoreError as error:
                # Identity still authenticates metering, never expired/revoked content.
                row = s.one(
                    "SELECT exploration_requests.request_id AS request_id, "
                    "exploration_requests.request_key AS request_key, "
                    "exploration_requests.adapter_id AS adapter_id, exploration_requests.operation "
                    "AS operation, exploration_requests.url AS url, "
                    "exploration_requests.goal_step_json AS goal_step_json, "
                    "exploration_requests.state AS state, exploration_requests.lease_token AS "
                    "lease_token, exploration_requests.lease_until AS lease_until, "
                    "exploration_requests.captured_privacy_epoch AS captured_privacy_epoch, "
                    "exploration_requests.reserved_bytes AS reserved_bytes, "
                    "exploration_requests.used_bytes AS used_bytes, "
                    "exploration_requests.result_sha256 AS result_sha256, "
                    "exploration_requests.last_error_code AS last_error_code, "
                    "exploration_requests.created_at AS created_at, "
                    "exploration_requests.started_at AS started_at, "
                    "exploration_requests.updated_at AS updated_at, exploration_requests.redacted "
                    "AS redacted, exploration_requests.reserved_requests AS reserved_requests, "
                    "exploration_requests.used_requests AS used_requests, "
                    "exploration_requests.source_grant_id AS source_grant_id, "
                    "exploration_requests.source_grant_epoch AS source_grant_epoch, "
                    "exploration_requests.authority_json AS authority_json, "
                    "exploration_requests.query AS query, exploration_requests.discovery_mode AS "
                    "discovery_mode FROM exploration_requests AS exploration_requests WHERE "
                    "exploration_requests.request_id = %s",
                    (request_id,),
                )
                outcome = {
                    "state": "CANCELLED"
                    if result.used_requests is not None and result.used_bytes is not None
                    else "UNKNOWN",
                    "event_ids": [],
                    "duplicate": False,
                }
                code = error.code
            else:
                self._evidence(attempt, row, context, result)
                step = json.loads(row["goal_step_json"])
                legacy = ReadResult(
                    lease_token=result.lease_token,
                    status="UNKNOWN" if result.status == "CANCELLED" else result.status,
                    source_grant=result.source_grant,
                    used_requests=result.used_requests,
                    used_bytes=result.used_bytes,
                    error_code=result.error_code,
                    items=[
                        ReadItem(
                            title=item.title,
                            text=item.text,
                            url=item.url,
                            reading=item.evidence
                            if isinstance(item.evidence, ReadingEvidence)
                            else None,
                            article=item.evidence
                            if isinstance(item.evidence, ArticleEvidence)
                            else None,
                            image_urls=[image.url for image in item.evidence.images]
                            if isinstance(item.evidence, ArticleEvidence)
                            else [],
                        )
                        for item in result.observations
                    ],
                )
                outcome = self.exploration.complete(adapter_id, request_id, legacy, reading=True)
                if result.status == "CANCELLED":
                    outcome["state"] = "CANCELLED"
                for event_id, item in zip(outcome["event_ids"], result.observations, strict=False):
                    stored = s.one(
                        "SELECT evidence_json FROM observations WHERE event_id = %s", (event_id,)
                    )
                    evidence = json.loads(stored["evidence_json"])
                    evidence["read_observation"] = item.model_dump(
                        mode="json",
                        exclude={"text", "title"},
                    )
                    s.execute(
                        "UPDATE observations SET evidence_json = %s WHERE event_id = %s",
                        (encode(evidence), event_id),
                    )
                code = result.error_code
            # Uncertain usage retains the reservation. Oversized receipts cannot lower it.
            used_requests = result.used_requests
            used_bytes = result.used_bytes
            if (
                used_requests is not None
                and used_requests > row["reserved_requests"]
                or used_bytes is not None
                and used_bytes > row["reserved_bytes"]
            ):
                used_requests, used_bytes = None, None
                outcome["state"] = "UNKNOWN"
            s.execute(
                "UPDATE exploration_requests SET state = %s, used_requests = %s, used_bytes = %s, "
                "result_sha256 = %s, last_error_code = %s, updated_at = %s WHERE request_id = %s",
                (
                    outcome["state"],
                    used_requests,
                    used_bytes,
                    fingerprint,
                    code,
                    self.clock(),
                    request_id,
                ),
            )
            s.execute(
                "UPDATE reading_attempts SET state = %s, result_sha256 = %s, result_event_ids_json "
                "= %s, observed_requests = %s, observed_bytes = %s, updated_at = %s WHERE "
                "request_id = %s",
                (
                    outcome["state"],
                    fingerprint,
                    encode(outcome["event_ids"]),
                    result.observed_requests,
                    result.observed_bytes,
                    self.clock(),
                    request_id,
                ),
            )
            # Wake the original Core worker after committing the correlated Reader result.
            s.execute(
                "UPDATE jobs SET next_attempt_at = %s, updated_at = %s WHERE state = "
                "'RETRY_WAIT' AND job_id IN (SELECT a.job_id FROM agent_tool_calls AS a JOIN "
                "agent_conversations AS c USING (job_id) WHERE a.request_id = %s AND a.state = "
                "'DEFERRED' AND c.phase = 'WAITING')",
                (self.clock(), self.clock(), request_id),
            )
            video = getattr(self, "video", None)
            if (
                not owner_authority.required(s, self.settings)
                and video is not None
                and self.settings.bili_video.enabled
            ) and (outcome["state"] == "DONE"):
                for content in s.rows(
                    "SELECT item_id FROM content_items WHERE read_request_id = "
                    "%s AND kind = 'link' AND redacted = 0",
                    (request_id,),
                ):
                    for event_id in outcome["event_ids"]:
                        source = s.one("SELECT source FROM events WHERE event_id = %s", (event_id,))
                        if source["source"] != "observation.bilibili":
                            continue
                        try:
                            video.schedule(content["item_id"], event_id)
                        except CoreError as error:
                            s.execute(
                                "UPDATE content_items SET error_code = %s, updated_at = %s "
                                "WHERE item_id = %s",
                                (error.code, self.clock(), content["item_id"]),
                            )
                            s.audit(
                                "bili_video.unavailable",
                                content["item_id"],
                                request_id,
                                self.clock(),
                                error_code=error.code,
                            )
        if step and result.status != "SUCCESS":
            self.exploration.goals.fail(
                step,
                code or "READ_FAILED",
                retryable=True,
                execution_uncertain=(
                    result.status == "UNKNOWN"
                    or outcome["state"] == "UNKNOWN"
                    or result.status == "CANCELLED"
                    and (used_requests is None or used_bytes is None)
                ),
            )
        if step and step["kind"] == "self":
            for event_id in outcome["event_ids"]:
                self.exploration.social.consider(event_id)
        return outcome
