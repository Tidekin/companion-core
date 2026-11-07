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
    from companion.exploration import ExplorationService
    from companion.goals import GoalEngine
    from companion.observation import ObservationService
    from companion.privacy import ErasureLedger
    from companion.social import SocialPlanner

    observations = ObservationService(store, settings, clock)
    goals = GoalEngine(store, settings, ErasureLedger(settings.erasure_ledger), clock)
    social = SocialPlanner(store, settings, clock)
    exploration = ExplorationService(store, settings, observations, goals, social, clock)
    video = BiliVideoService(store, settings, exploration, clock=clock)
    return ReadingService(store, settings, exploration, clock, video=video)


def followup_item(store, settings, event_id, *, clock=utcnow):
    """Project a source-bound SDK tool selection through the existing Content view."""
    if settings.owner_runtime.mode != "ENABLED" or not store.one(
        "SELECT 1 FROM sqlite_master WHERE name='agent_tool_calls'"
    ):
        return None
    item = store.one(
        "SELECT c.* FROM content_items c JOIN exploration_requests r "
        "ON r.request_id=c.read_request_id JOIN agent_tool_calls a "
        "ON a.request_id=r.request_id JOIN jobs j ON j.job_id=a.job_id "
        "JOIN agent_conversations ac ON ac.job_id=j.job_id WHERE j.event_id=? "
        "AND ac.phase<>'STOPPED' AND a.state IN ('DEFERRED','RETURNED') "
        "AND c.kind='link' AND c.redacted=0 AND r.redacted=0 AND r.operation='read' "
        "AND r.goal_step_json='{}' ORDER BY a.rowid DESC LIMIT 1", (event_id,),
    )
    if not item:
        return None
    try:
        job = store.one("SELECT * FROM jobs WHERE event_id=? AND kind='chat'", (event_id,))
        view = owner_authority.check_job(store, settings, clock, job, purpose="content")
        reply_revision.check_job(store, job)
        consumer, _, _ = owner_input.source(store, settings, view, event_id, clock())
        source, _, _ = owner_input.source(store, settings, view, item["owner_event_id"],
                                          clock(), history=item["owner_event_id"] != event_id)
        if (source["conversation_id"] != consumer["conversation_id"]
            or not reply_revision.source_active(store, source["event_id"])):
            return None
    except CoreError:
        return None
    return item


def erase_reading(store, event_ids):
    if not store.one("SELECT 1 FROM sqlite_master WHERE name='reading_attempts'"):
        return
    clear = (
        "UPDATE reading_attempts SET state=CASE WHEN state='RUNNING' THEN 'UNKNOWN' "
        "ELSE state END,context_json='{}',allowed_hosts_json='[]',original_url_sha256='',"
        "source_grant_json='null',result_event_ids_json='[]' "
    )
    # A forgotten grant may own only an in-flight read, with no observation/event roots yet.
    # Consult its durable tombstone on both initial erasure and old-backup replay.
    store.execute(clear + "WHERE request_id IN (SELECT request_id FROM exploration_requests x "
                  "WHERE EXISTS (SELECT 1 FROM runtime_meta "
                  "WHERE key='erased_source_grant:' || x.source_grant_id))")
    for event_id in event_ids:
        store.execute(
            "DELETE FROM runtime_meta WHERE key=? OR (key LIKE ? AND "
            "json_extract(value,'$.item_id') IN "
            "(SELECT item_id FROM content_items WHERE owner_event_id=?))",
            (FOLLOWUP + event_id, FOLLOWUP + "%", event_id),
        )
        # Retain only the attempt identity/counters: a late receipt cannot revive content.
        store.execute(clear + "WHERE request_id IN "
            "(SELECT request_id FROM exploration_evidence WHERE event_id=?) OR "
            "EXISTS (SELECT 1 FROM json_each(context_json,'$.source_event_ids') WHERE value=?)",
            (event_id, event_id),
        )


def _validate_sources(store, settings, clock, exploration, row, context):
    exploration.goals.validate_sources(context.source_event_ids)
    for source_id in context.source_event_ids:
        if not reply_revision.source_active(store, source_id):
            raise CoreError("REPLY_SOURCE_RECALLED", 409)
        captured_owner = store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                                   (owner_input.INPUT + source_id,))
        if row["goal_step_json"] == "{}" and (
            captured_owner or reply_revision.owner_input(store, source_id)
        ):
            if settings.owner_runtime.mode == "ENABLED":
                view = owner_authority.current(store, settings, clock,
                                               purpose="content")
                owner_input.source(store, settings, view, source_id, clock())
            else:
                reply_revision.require_current(store, source_id)


def reading_attempt(store, adapter_id, request_id, token):
    attempt = store.one(
        "SELECT * FROM reading_attempts WHERE request_id=? AND adapter_id=?",
        (request_id, adapter_id),
    )
    if not attempt or attempt["lease_token"] != token:
        raise CoreError("STALE_READER_LEASE", 409)
    return attempt


def validate_attempt(store, settings, clock, exploration, attempt, validation, *,
                     current_request=None):
    if attempt["state"] != "RUNNING" or attempt["lease_until"] <= clock():
        raise CoreError("STALE_READER_LEASE", 409)
    if attempt["context_json"] == "{}":
        raise CoreError("PRIVACY_CHANGED", 409)
    context = ReadingContext.model_validate_json(attempt["context_json"])
    if context.privacy_epoch != epoch(store):
        raise CoreError("PRIVACY_CHANGED", 409)
    exploration.validate_read(
        attempt["adapter_id"], attempt["request_id"], validation, reading=True,
        current_request=current_request,
    )
    row = store.one("SELECT * FROM exploration_requests WHERE request_id=?",
                (attempt["request_id"],))
    if hashlib.sha256(row["url"].encode()).hexdigest() != attempt["original_url_sha256"]:
        raise CoreError("READER_TARGET_MISMATCH", 409)
    _validate_sources(store, settings, clock, exploration, row, context)
    paused = store.one("SELECT value FROM runtime_meta WHERE key='life_paused'")
    if paused and paused["value"] == "1" and exploration.autonomous(
        row, json.loads(row["goal_step_json"]),
    ):
        raise CoreError("AUTONOMY_PAUSED", 409)
    policy = exploration.adapter(attempt["adapter_id"])
    if policy.source != context.source or (
        policy.source == "web" and not set(json.loads(attempt["allowed_hosts_json"]))
        <= set(settings.web_reading_hosts)
    ):
        raise CoreError("READER_SCOPE_CHANGED", 409)
    return row, context


class ReadingService:
    def __init__(self, store, settings, exploration, clock=utcnow, *, video=None):
        self.video = video
        self.store, self.settings, self.exploration, self.clock = (
            store, settings, exploration, clock,
        )

    def _context(self, row, source):
        sources = [item["event_id"] for item in self.store.rows(
            "SELECT event_id FROM exploration_evidence WHERE request_id=? ORDER BY event_id",
            (row["request_id"],),
        )]
        step = json.loads(row["goal_step_json"])
        root = step.get("goal_id", row["request_id"])
        if not step:
            job = self.store.one("SELECT job_id FROM agent_tool_calls WHERE request_id=? "
                                 "ORDER BY rowid LIMIT 1", (row["request_id"],))
            if job is None:
                job = self.store.one(
                    "SELECT job_id FROM jobs WHERE kind='chat' AND event_id IN "
                    "(SELECT event_id FROM exploration_evidence WHERE request_id=?) ORDER BY rowid",
                    (row["request_id"],),
                )
            if job:
                root = job["job_id"]
        return ReadingContext(root_task_id=root, source=source, source_event_ids=sources,
                              privacy_epoch=row["captured_privacy_epoch"])

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
                    return {"max_items": 1, "max_requests": 1,
                            "deadline": min(after(self.clock(), 30),
                                after(row["created_at"], TARGET_TTL_SECONDS),
                                captured.snapshot()["expires_at"])}
                video = self.video
                exists = s.one("SELECT 1 FROM sqlite_master WHERE name='bili_video_items'")
                if not exists or not s.one("SELECT 1 FROM bili_video_items WHERE request_id=?",
                                          (row["request_id"],)):
                    return None
                if video is None or "bounded_video_media_v1" not in capabilities:
                    return False
                return {**video.lease_limits(row["request_id"], source_grant), "max_items": 1}

            item = self.exploration.lease(
                adapter_id, source_grant, reading=True,
                source_check=lambda row: _validate_sources(
                    s, self.settings, self.clock, self.exploration, row,
                    self._context(row, policy.source)),
                request_limits=request_limits,
            )
            if item is None:
                return None
            row = s.one("SELECT * FROM exploration_requests WHERE request_id=?",
                        (item["request_id"],))
            context = self._context(row, policy.source)
            _validate_sources(self.store, self.settings, self.clock, self.exploration, row, context)
            hosts = list(self.settings.web_reading_hosts) if policy.source == "web" else []
            if row["operation"] == "read":
                source, canonical = content_target(row["url"], web_hosts=hosts)
                if source != policy.source or canonical != row["url"]:
                    raise CoreError("READER_TARGET_MISMATCH", 400)
            elif policy.source == "web":
                raise CoreError("READER_OPERATION_UNAVAILABLE", 403)
            s.insert(
                "reading_attempts", request_id=row["request_id"], adapter_id=adapter_id,
                lease_token=item["lease_token"], lease_until=row["lease_until"],
                context_json=encode(context.model_dump()), allowed_hosts_json=encode(hosts),
                original_url_sha256=hashlib.sha256(row["url"].encode()).hexdigest(),
                source_grant_json=encode(item.get("source_grant")), state="RUNNING",
                created_at=self.clock(), updated_at=self.clock(),
            )
            result = {**item, "schema_version": 1, "lease_until": row["lease_until"],
                      "context": context.model_dump(), "allowed_hosts": hosts}
            video = self.video
            return video.bind_lease(result) if video is not None else result

    def validate_read(self, adapter_id, request_id, validation):
        with self.store.transaction():
            attempt = reading_attempt(self.store, adapter_id, request_id, validation.lease_token)
            validate_attempt(self.store, self.settings, self.clock, self.exploration,
                                 attempt, validation)
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
        if (result.used_requests is not None and result.used_requests > row["reserved_requests"]
            or result.used_bytes is not None and result.used_bytes > row["reserved_bytes"]):
            raise CoreError("READER_USAGE_LIMIT", 400)
        if len(result.observations) > min(40, self.settings.exploration_daily_candidate_limit):
            raise CoreError("READER_ITEM_LIMIT", 400)
        for observation in result.observations:
            provenance = observation.provenance.model_dump(exclude={"source_grant"})
            if provenance != context.model_dump():
                raise CoreError("READING_PROVENANCE_MISMATCH", 400)
            if row["operation"] == "read" and hashlib.sha256(
                observation.original_url.encode(),
            ).hexdigest() != attempt["original_url_sha256"]:
                raise CoreError("READER_TARGET_MISMATCH", 400)
            for original, url in ((True, observation.original_url), (False, observation.url)):
                source, canonical = content_target(
                    url, web_hosts=json.loads(attempt["allowed_hosts_json"]),
                )
                if source != context.source or canonical != url or (
                    row["operation"] == "read" and canonical != row["url"]
                    and (original or context.source != "web")
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
            evidence_bytes = (sum(item.bytes for item in evidence.sources)
                              if isinstance(evidence, ReadingEvidence)
                              else getattr(evidence, "response_bytes", 0))
            if result.used_bytes is None or evidence_bytes > result.used_bytes:
                raise CoreError("READING_EVIDENCE_USAGE_MISMATCH", 400)

    def complete(self, adapter_id, request_id, result):
        s, fingerprint, step = self.store, digest(result.model_dump(mode="json")), None
        with s.transaction():
            if s.one("SELECT 1 FROM sqlite_master WHERE name='bili_video_items'") and s.one(
                "SELECT 1 FROM bili_video_items WHERE request_id=?", (request_id,),
            ):
                raise CoreError("READER_MEDIA_PROTOCOL_REQUIRED", 409)
            attempt = reading_attempt(self.store, adapter_id, request_id, result.lease_token)
            if attempt["result_sha256"]:
                if attempt["result_sha256"] != fingerprint:
                    raise CoreError("IDEMPOTENCY_CONFLICT", 409)
                return {"state": attempt["state"], "duplicate": True,
                        "event_ids": json.loads(attempt["result_event_ids_json"])}
            validation = ReaderReadValidation(
                lease_token=result.lease_token, source_grant=result.source_grant,
            )
            try:
                row, context = validate_attempt(
                    s, self.settings, self.clock, self.exploration, attempt, validation)
            except CoreError as error:
                # Identity still authenticates metering, never expired/revoked content.
                row = s.one("SELECT * FROM exploration_requests WHERE request_id=?", (request_id,))
                outcome = {"state": "CANCELLED" if result.used_requests is not None
                           and result.used_bytes is not None else "UNKNOWN", "event_ids": [],
                           "duplicate": False}
                code = error.code
            else:
                self._evidence(attempt, row, context, result)
                step = json.loads(row["goal_step_json"])
                legacy = ReadResult(
                    lease_token=result.lease_token,
                    status="UNKNOWN" if result.status == "CANCELLED" else result.status,
                    source_grant=result.source_grant, used_requests=result.used_requests,
                    used_bytes=result.used_bytes, error_code=result.error_code,
                    items=[ReadItem(
                        title=item.title, text=item.text, url=item.url,
                        reading=item.evidence if isinstance(item.evidence, ReadingEvidence)
                        else None,
                        article=item.evidence if isinstance(item.evidence, ArticleEvidence)
                        else None,
                        image_urls=[image.url for image in item.evidence.images]
                        if isinstance(item.evidence, ArticleEvidence) else [],
                    ) for item in result.observations],
                )
                outcome = self.exploration.complete(adapter_id, request_id, legacy, reading=True)
                if result.status == "CANCELLED":
                    outcome["state"] = "CANCELLED"
                for event_id, item in zip(outcome["event_ids"], result.observations, strict=False):
                    stored = s.one("SELECT evidence_json FROM observations WHERE event_id=?",
                                   (event_id,))
                    evidence = json.loads(stored["evidence_json"])
                    evidence["read_observation"] = item.model_dump(
                        mode="json", exclude={"text", "title"},
                    )
                    s.execute("UPDATE observations SET evidence_json=? WHERE event_id=?",
                              (encode(evidence), event_id))
                code = result.error_code
            # Uncertain usage retains the reservation. Oversized receipts cannot lower it.
            used_requests = result.used_requests
            used_bytes = result.used_bytes
            if (used_requests is not None and used_requests > row["reserved_requests"]
                or used_bytes is not None and used_bytes > row["reserved_bytes"]):
                used_requests, used_bytes = None, None
                outcome["state"] = "UNKNOWN"
            s.execute(
                "UPDATE exploration_requests SET state=?,used_requests=?,used_bytes=?,"
                "result_sha256=?,last_error_code=?,updated_at=? WHERE request_id=?",
                (outcome["state"], used_requests, used_bytes, fingerprint, code,
                 self.clock(), request_id),
            )
            s.execute(
                "UPDATE reading_attempts SET state=?,result_sha256=?,result_event_ids_json=?,"
                "observed_requests=?,observed_bytes=?,updated_at=? WHERE request_id=?",
                (outcome["state"], fingerprint, encode(outcome["event_ids"]),
                 result.observed_requests, result.observed_bytes, self.clock(), request_id),
            )
            # Wake the original Core worker after committing the correlated Reader result.
            s.execute("UPDATE jobs SET next_attempt_at=?,updated_at=? WHERE state='RETRY_WAIT' "
                      "AND job_id IN (SELECT a.job_id FROM agent_tool_calls a "
                      "JOIN agent_conversations c USING(job_id) WHERE a.request_id=? "
                      "AND a.state='DEFERRED' AND c.phase='WAITING')",
                      (self.clock(), self.clock(), request_id))
            video = self.video
            if (not owner_authority.required(s, self.settings)
                and video is not None and self.settings.bili_video.enabled) and (
                outcome["state"] == "DONE"
            ):
                for content in s.rows("SELECT item_id FROM content_items WHERE read_request_id=? "
                                      "AND kind='link' AND redacted=0", (request_id,)):
                    for event_id in outcome["event_ids"]:
                        source = s.one("SELECT source FROM events WHERE event_id=?", (event_id,))
                        if source["source"] != "observation.bilibili":
                            continue
                        try:
                            video.schedule(content["item_id"], event_id)
                        except CoreError as error:
                            s.execute("UPDATE content_items SET error_code=?,updated_at=? "
                                      "WHERE item_id=?", (error.code, self.clock(),
                                                         content["item_id"]))
                            s.audit("bili_video.unavailable", content["item_id"], request_id,
                                    self.clock(), error_code=error.code)
        if step and result.status != "SUCCESS":
            self.exploration.goals.fail(step, code or "READ_FAILED", retryable=True,
                execution_uncertain=(result.status == "UNKNOWN" or outcome["state"] == "UNKNOWN"
                    or result.status == "CANCELLED"
                    and (used_requests is None or used_bytes is None)))
        if step and step["kind"] == "self":
            for event_id in outcome["event_ids"]:
                self.exploration.social.consider(event_id)
        return outcome
