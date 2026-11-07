"""Durable, metered GET-only work handed to authenticated source adapters."""

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import Field

from companion.contracts import CoreError, StrictModel
from companion.goals import GoalProposal, GoalScope
from companion.observation import (
    ArticleEvidence,
    ObservationEvent,
    ObservationPayload,
    ReadingEvidence,
)
from companion.privacy import epoch
from companion.source_grants import SourceGrantSnapshot
from companion.source_policy import web_host_allowed
from companion.sources import content_target, observation_sources, supports, web_hosts
from companion.store import after, encode, uid, utcnow
from companion.transport import digest
from companion.wake import IDLE_DISCOVERY

METERED_READS_SQL = (

        'SELECT x.*,x.record_sequence AS queue_order,'
        'a.observed_requests,a.observed_bytes,'
        'GREATEST(COALESCE(x.used_requests,x.reserved_requests),'
        'COALESCE(a.observed_requests,0)) AS accounted_requests,'
        'GREATEST(COALESCE(x.used_bytes,x.reserved_bytes),'
        'COALESCE(a.observed_bytes,0)) AS accounted_bytes FROM '
        'exploration_requests x LEFT JOIN reading_attempts a '
        'USING(request_id)'

)
METERED_USAGE_SQL = (
    "SELECT started_at,operation,accounted_requests,accounted_bytes FROM ("
    + METERED_READS_SQL
    + ") UNION ALL SELECT source_budget_at,'read',"
    "COALESCE(source_used_requests,source_reserved_requests),"
    "COALESCE(source_used_bytes,source_reserved_bytes) FROM voice_jobs "
    "WHERE operation='sing' AND source_reserved_requests>0"
)


def read_periods(settings, now):
    local = datetime.fromisoformat(now).astimezone(ZoneInfo(settings.timezone))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return tuple(
        at.astimezone(UTC).isoformat(timespec="microseconds") for at in (day, day.replace(day=1))
    )


class ReadItem(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(default="", max_length=12000)
    url: str = Field(min_length=1, max_length=2048)
    topic: str = Field(default="", max_length=80)
    reading: ReadingEvidence | None = None
    article: ArticleEvidence | None = None
    image_urls: list[Annotated[str, Field(max_length=2048)]] = Field(
        default_factory=list, max_length=32
    )


class ReadResult(StrictModel):
    lease_token: str = Field(min_length=8, max_length=128)
    status: Literal["SUCCESS", "FAILED", "UNKNOWN"]
    items: list[ReadItem] = Field(default_factory=list, max_length=40)
    used_bytes: int | None = Field(default=None, ge=0, le=1048576)
    used_requests: int | None = Field(default=None, ge=0, le=8)
    error_code: str | None = Field(default=None, pattern=r"^[A-Z0-9_]{1,64}$")
    source_grant: SourceGrantSnapshot | None = None


def erase_exploration(store, event_ids):
    if not store.one(
        "SELECT 1 FROM pg_catalog.pg_class WHERE "
        "relnamespace=current_schema()::regnamespace AND relname='exploration_requests' AND "
        "relkind IN ('r','p')"
    ):
        return
    for event_id in event_ids:
        store.execute(
            "UPDATE exploration_requests SET url = '', goal_step_json = '{}', lease_token = NULL, "
            "state = 'CANCELLED', redacted = 1, result_sha256 = NULL WHERE request_id IN (SELECT "
            "request_id FROM exploration_evidence WHERE event_id = %s)",
            (event_id,),
        )
        store.execute("DELETE FROM exploration_evidence WHERE event_id = %s", (event_id,))
        store.execute(
            "UPDATE social_candidates SET comparison_text = '', canonical_url = '', content_sha256 "
            "= '', state = 'ERASED', reason_code = 'PRIVACY_CHANGED' WHERE event_id = %s",
            (event_id,),
        )


class ExplorationService:
    def __init__(
        self, store, settings, observations, goals, social, clock=utcnow, *, content_options=None,
    ):
        self.store, self.settings, self.clock = store, settings, clock
        self.observations, self.goals, self.social = observations, goals, social
        self.content_options = content_options

    def _read_scope(self, row, *, current_request=None):
        """Recheck the current signed card before bounded Reader work can proceed."""
        from companion import live_text, owner_authority, qq_source

        if self.adapter(row["adapter_id"]).source == "qzone":
            from companion.qzone import validate_target

            validate_target(self.store, self.settings, self.clock, row)
        if owner_authority.required(self.store, self.settings):
            ref = json.loads(row["authority_json"])
            owner_authority.current(
                self.store,
                self.settings,
                self.clock,
                purpose="content" if row["operation"] == "read" else "exploration",
                authority=ref,
            )
            step = json.loads(row["goal_step_json"])
            if step:
                self.goals.validate_authority(self.goals.get(step["goal_id"]))
            return

        if not live_text.active(self.store, self.settings):
            return
        approval, _, _ = live_text.guard(
            self.store,
            self.settings,
            self.clock,
            current_request=current_request,
        )
        if approval.companion_scope == "owner_private_v1":
            return  # Native goal/source/adapter and resource leases below still apply.
        if (
            getattr(approval, "link_scope", "none") != "owner_links_v1"
            or row["operation"] != "read"
            or row["goal_step_json"] != "{}"
        ):
            raise CoreError("LIVE_LINK_SCOPE_REQUIRED", 403)
        items = self.store.rows(
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
            "WHERE content_items.kind = 'link' AND content_items.read_request_id = %s AND "
            "content_items.redacted = 0",
            (row["request_id"],),
        )
        if len(items) != 1:
            raise CoreError("LIVE_LINK_SOURCE_REQUIRED", 403)
        item = items[0]
        _, body, job = qq_source.source(
            self.store,
            self.settings,
            approval,
            item["owner_event_id"],
        )
        urls = {
            field["value"]
            for segment in (body.get("rich") or {}).get("segments", [])
            if segment.get("type") == "json"
            for field in segment.get("fields", [])
            if field.get("kind") == "url"
        }
        sources = self.store.rows(
            "SELECT event_id FROM exploration_evidence WHERE request_id = %s",
            (row["request_id"],),
        )
        if (
            job["state"] != "QUEUED"
            or item["state"] != "WAITING"
            or item["deadline"] <= self.clock()
            or item["captured_privacy_epoch"] != epoch(self.store)
            or row["captured_privacy_epoch"] != epoch(self.store)
            or item["target_url"] != row["url"]
            or row["url"] not in urls
            or sources != [{"event_id": item["owner_event_id"]}]
        ):
            raise CoreError("LIVE_LINK_SOURCE_CHANGED", 403)
        source, canonical = content_target(row["url"], web_hosts=web_hosts(self.settings))
        if canonical != row["url"] or source != self.adapter(row["adapter_id"]).source:
            raise CoreError("LIVE_LINK_SOURCE_CHANGED", 403)

    def adapter(self, adapter_id):
        policy = self.observations.policy(adapter_id)
        content_enabled = self.settings.content_enabled
        if (
            (not self.settings.exploration_enabled and not content_enabled)
            or (not supports(policy.source, "get"))
            or (
                policy.source == "web"
                and (not self.settings.web_reading_enabled or not self.settings.web_reading_hosts)
            )
        ):
            raise CoreError("EXPLORATION_DISABLED", 403)
        return policy

    def periods(self):
        return read_periods(self.settings, self.clock())

    def autonomous(self, request, step):
        if step:
            return step["kind"] == "self"
        if request["operation"] != "read":
            return True
        return not self.store.one(
            "SELECT 1 FROM exploration_evidence AS x JOIN events AS e USING (event_id) WHERE "
            "x.request_id = %s AND e.source = 'qq.onebot' AND e.actor_id = 'owner' AND e.redacted "
            "= 0",
            (request["request_id"],),
        )

    def create(
        self, adapter_id, operation, key, url="", step=None, source_event_ids=None, query=None
    ):
        """`query` is a Core-chosen discover search term; only discover requests carry one."""
        policy = self.adapter(adapter_id)
        if policy.source == "qzone" and (
            operation != "read" or not key.startswith("qzone:") or step or source_event_ids
        ):
            raise CoreError("QZONE_TARGET_SELECTION_REQUIRED", 403)
        if query is not None and (operation != "discover" or not 2 <= len(query) <= 100):
            raise CoreError("EXPLORATION_QUERY_INVALID", 400)
        s, now = self.store, self.clock()
        previous = s.one(
            "SELECT request_id FROM exploration_requests WHERE request_key = %s", (key,)
        )
        if previous:
            return previous["request_id"]
        sources = self.goals.step_source_ids(step["step_id"]) if step else source_event_ids or []
        self.goals.validate_sources(sources)
        from companion import owner_authority

        authority = {}
        if owner_authority.required(s, self.settings):
            purpose = "content" if operation == "read" else "exploration"
            if step:
                goal = self.goals.get(step["goal_id"])
                self.goals.validate_authority(goal)
                authority = json.loads(goal["authority_json"])
            else:
                # Owner input carries the original admission; never upgrade an old turn.
                for source_id in sources:
                    job = s.one(
                        "SELECT jobs.job_id AS job_id, jobs.event_id AS event_id, "
                        "jobs.kind AS kind, jobs.state AS state, jobs.idempotency_key AS "
                        "idempotency_key, jobs.attempts AS attempts, jobs.max_attempts AS "
                        "max_attempts, jobs.lease_owner AS lease_owner, jobs.lease_until AS "
                        "lease_until, jobs.next_attempt_at AS next_attempt_at, "
                        "jobs.last_error_code AS last_error_code, jobs.created_at AS created_at, "
                        "jobs.updated_at AS updated_at, jobs.lease_token AS lease_token, "
                        "jobs.context_tracked AS context_tracked, jobs.authority_json AS "
                        "authority_json FROM jobs AS jobs WHERE jobs.event_id = %s AND jobs.kind = "
                        "'chat'",
                        (source_id,),
                    )
                    if job:
                        view = owner_authority.check_job(
                            s, self.settings, self.clock, job, purpose=purpose
                        )
                        if authority and authority != view.authority.as_dict():
                            raise CoreError("OWNER_AUTHORITY_REVISION_CHANGED", 409)
                        authority = view.authority.as_dict()
            view = owner_authority.current(
                s, self.settings, self.clock, purpose=purpose, authority=authority or None
            )
            authority = view.authority.as_dict()
        request_id = uid("read")
        s.insert(
            "exploration_requests",
            request_id=request_id,
            request_key=key,
            adapter_id=adapter_id,
            operation=operation,
            url=url,
            goal_step_json=encode(step or {}),
            state="QUEUED",
            captured_privacy_epoch=epoch(s),
            created_at=now,
            updated_at=now,
            authority_json=encode(authority),
            query=query,
            discovery_mode=(
                "recommend"
                if operation == "discover"
                and query is None
                and policy.bilibili_discovery == "recommend"
                else "default"
            ),
        )
        for source_id in set(sources):
            s.insert("exploration_evidence", request_id=request_id, event_id=source_id)
        return request_id

    def schedule_wake(self, decisions):
        if not self.settings.exploration_enabled or not self.settings.observation_enabled:
            return
        adapters = [
            name
            for name, adapter in self.settings.observation_adapters.items()
            if supports(adapter.source, "discover")
        ]
        if not adapters:
            return
        from companion import topic_selection

        for decision in decisions:
            if decision["decision"] == "OBSERVE":
                with nullcontext() if self.store.in_transaction else self.store.transaction():
                    idle = decision.get("reason_code") == IDLE_DISCOVERY
                    pool = [
                        name
                        for name in adapters
                        if not idle or self.settings.observation_adapters[name].source == "bilibili"
                    ]
                    if not pool:
                        continue
                    chosen = topic_selection.choose(
                        self.store, self.settings, self.clock(), pool, own_only=idle
                    )
                    if chosen:
                        self.create(chosen[0], "discover", decision["decision_id"], query=chosen[1])
                        continue
                    count = self.store.one(
                        "SELECT COUNT(*) AS n FROM exploration_requests WHERE operation = "
                        "'discover' AND query IS NULL"
                    )["n"]
                    self.create(pool[count % len(pool)], "discover", decision["decision_id"])

    def schedule_goal(self):
        self.recover()
        if not self.budget_available(for_read=True):
            return False
        step = self.goals.claim()
        if not step:
            return False
        try:
            scope = GoalScope.model_validate_json(step["scope_json"])
            source, url = content_target(scope.url, web_hosts=web_hosts(self.settings))
            adapter_id = next(
                (
                    name
                    for name, adapter in self.settings.observation_adapters.items()
                    if adapter.source == source
                    and (
                        source != "web"
                        or web_host_allowed(urlsplit(url).hostname, self.settings.web_reading_hosts)
                    )
                ),
                None,
            )
            if not adapter_id or not self.settings.observation_enabled:
                self.goals.fail(step, "READER_UNAVAILABLE", resource=True)
                return False
            with self.store.transaction():
                self.goals.validate_lease(step)
                self.create(adapter_id, "read", step["lease_token"], url, step)
            return True
        except CoreError as error:
            self.goals.fail(step, error.code)
            return False

    def recover(self):
        s, now = self.store, self.clock()
        with nullcontext() if s.in_write_transaction else s.transaction():
            for row in s.rows(
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
                "exploration_requests AS exploration_requests WHERE exploration_requests.state IN "
                "('QUEUED', 'RUNNING')",
            ):
                step = json.loads(row["goal_step_json"])
                expired = row["lease_until"] and row["lease_until"] <= now
                if row["state"] == "QUEUED" and step:
                    current = s.one(
                        "SELECT lease_until FROM goal_steps WHERE step_id = %s", (step["step_id"],)
                    )
                    expired = not current or current["lease_until"] <= now
                if not expired:
                    continue
                was_started = row["state"] == "RUNNING"
                changed = s.execute(
                    "UPDATE exploration_requests SET state = %s, last_error_code = %s, updated_at "
                    "= %s WHERE request_id = %s AND state = %s",
                    (
                        "UNKNOWN" if was_started else "CANCELLED",
                        "READ_TIMEOUT" if was_started else "READER_UNAVAILABLE",
                        now,
                        row["request_id"],
                        row["state"],
                    ),
                )
                if changed and step:
                    self.goals.fail(
                        step,
                        "READ_TIMEOUT" if was_started else "READER_UNAVAILABLE",
                        execution_uncertain=was_started,
                        resource=not was_started,
                    )
            # Also repair parent-only timeouts left by older workers. Keep the original
            # lease identity and reservations so authenticated late usage can still settle.
            s.execute(
                "UPDATE reading_attempts SET state = 'UNKNOWN', updated_at = %s WHERE state "
                "= 'RUNNING' AND lease_until <= %s",
                (now, now),
            )

    def budget_available(self, for_read=False):
        cfg, s = self.settings, self.store
        day, month = self.periods()
        row = s.one(
            "SELECT SUM(accounted_requests) AS monthly,"
            "SUM(CASE WHEN started_at>=%s THEN accounted_requests "
            "ELSE 0 END) AS daily,"
            "SUM(CASE WHEN started_at>=%s THEN accounted_bytes "
            "ELSE 0 END) bytes,"
            "SUM(CASE WHEN started_at>=%s AND operation='read' THEN 1 ELSE 0 END) reads "
            f"FROM ({METERED_USAGE_SQL}) WHERE started_at>=%s",
            (day, day, day, month),
        )
        return (
            (row["daily"] or 0) < cfg.exploration_daily_requests
            and (row["monthly"] or 0) < cfg.exploration_monthly_requests
            and (row["bytes"] or 0) + cfg.exploration_max_response_bytes
            <= cfg.exploration_daily_bytes
            and (not for_read or (row["reads"] or 0) < cfg.exploration_daily_deep_reads)
        )

    def lease(
        self,
        adapter_id,
        source_grant=None,
        *,
        reading=False,
        source_check=None,
        request_limits=None,
    ):
        from companion import live_text

        policy = self.adapter(adapter_id)
        if policy.source in {"web", "qzone"} and not reading:
            raise CoreError("READER_PROTOCOL_REQUIRED", 409)
        cfg, s = self.settings, self.store
        with nullcontext() if s.in_transaction else s.transaction():
            now = self.clock()
            day, month = self.periods()
            if source_grant or policy.source_grant_required:
                self.observations.grants.require(adapter_id, source_grant)
            self.recover()
            s.execute(
                "UPDATE exploration_requests SET state = 'CANCELLED', url = '', goal_step_json = "
                "'{}' WHERE state = 'QUEUED' AND captured_privacy_epoch <> %s",
                (epoch(s),),
            )
            counts = s.one(
                "SELECT SUM(CASE WHEN started_at>=%s THEN accounted_requests "
                "ELSE 0 END) AS daily,SUM(accounted_requests) AS monthly,"
                "SUM(CASE WHEN started_at>=%s THEN accounted_bytes ELSE 0 END) "
                "AS bytes,SUM(CASE WHEN started_at>=%s AND operation='read' "
                "THEN 1 ELSE 0 END) AS reads "
                f"FROM ({METERED_USAGE_SQL}) WHERE started_at>=%s",
                (day, day, day, month),
            )
            rows = s.rows(
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
                "exploration_requests.adapter_id = %s AND exploration_requests.state = 'QUEUED' "
                "ORDER BY CASE exploration_requests.operation WHEN 'read' THEN 0 ELSE 1 END NULLS "
                "FIRST, exploration_requests.created_at NULLS FIRST",
                (adapter_id,),
            )
            paused = s.one("SELECT value FROM runtime_meta WHERE key = 'life_paused'")
            for row in rows:
                if row["discovery_mode"] == "recommend":
                    if not reading:
                        raise CoreError("READER_PROTOCOL_REQUIRED", 409)
                    if policy.bilibili_discovery != "recommend":
                        s.execute(
                            "UPDATE exploration_requests SET state = 'CANCELLED', "
                            "last_error_code = 'DISCOVERY_MODE_CHANGED' WHERE request_id = %s",
                            (row["request_id"],),
                        )
                        continue
                    self.observations.grants.require(adapter_id, source_grant)
                limits = {
                    "max_bytes": cfg.exploration_max_response_bytes,
                    "max_requests": policy.reader_max_requests,
                    "max_items": min(40, cfg.exploration_daily_candidate_limit),
                    "deadline": after(now, 30),
                }
                if request_limits is not None:
                    try:
                        selected = request_limits(row)
                    except CoreError as error:
                        if error.status == 429:
                            continue
                        s.execute(
                            "UPDATE exploration_requests SET state = 'CANCELLED', "
                            "last_error_code = %s, updated_at = %s WHERE request_id = %s",
                            (error.code, now, row["request_id"]),
                        )
                        continue
                    if selected is False:
                        continue
                    if selected is not None:
                        limits.update(selected)
                if (
                    (counts["daily"] or 0) + limits["max_requests"] > cfg.exploration_daily_requests
                    or (counts["monthly"] or 0) + limits["max_requests"]
                    > cfg.exploration_monthly_requests
                    or (counts["bytes"] or 0) + limits["max_bytes"] > cfg.exploration_daily_bytes
                ):
                    continue
                if row["operation"] == "discover" and not cfg.exploration_enabled:
                    continue
                step = json.loads(row["goal_step_json"])
                if paused and paused["value"] == "1" and self.autonomous(row, step):
                    continue
                if (
                    row["operation"] == "read"
                    and (counts["reads"] or 0) >= cfg.exploration_daily_deep_reads
                ):
                    continue
                if step:
                    try:
                        self.goals.validate_lease(step)
                    except CoreError:
                        s.execute(
                            "UPDATE exploration_requests SET state = 'CANCELLED' WHERE request_id "
                            "= %s",
                            (row["request_id"],),
                        )
                        continue
                # The same input's image analysis owns the single paid-call
                # slot. Do not lease a card read that would lose validation
                # when the image starts its reserved vision request.
                if live_text.active(s, cfg) and s.one(
                    "SELECT 1 FROM content_items AS link JOIN content_items AS image ON "
                    "image.owner_event_id = link.owner_event_id WHERE link.kind = 'link' AND "
                    "link.read_request_id = %s AND image.kind = 'qq_image' AND image.redacted = 0 "
                    "AND image.state IN ('WAITING', 'QUEUED', 'FETCHING', 'RUNNING') LIMIT 1",
                    (row["request_id"],),
                ):
                    continue
                if source_check is not None:
                    try:
                        source_check(row)
                    except CoreError as error:
                        s.execute(
                            "UPDATE exploration_requests SET state = 'CANCELLED', last_error_code "
                            "= %s, updated_at = %s WHERE request_id = %s",
                            (error.code, now, row["request_id"]),
                        )
                        continue
                self._read_scope(row)
                token, until = uid("reader_lease"), limits["deadline"]
                if step:
                    goal_until = s.one(
                        "SELECT lease_until FROM goal_steps WHERE step_id = %s", (step["step_id"],)
                    )["lease_until"]
                    until = min(until, goal_until)
                s.execute(
                    "UPDATE exploration_requests SET state = 'RUNNING', lease_token = %s, "
                    "lease_until = %s, reserved_bytes = %s, reserved_requests = %s, started_at = "
                    "%s, updated_at = %s, source_grant_id = %s, source_grant_epoch = %s WHERE "
                    "request_id = %s",
                    (
                        token,
                        until,
                        limits["max_bytes"],
                        limits["max_requests"],
                        now,
                        now,
                        source_grant.grant_id if source_grant else None,
                        source_grant.epoch if source_grant else None,
                        row["request_id"],
                    ),
                )
                item = {
                    "request_id": row["request_id"],
                    "lease_token": token,
                    "operation": row["operation"],
                    "url": row["url"],
                    "max_bytes": limits["max_bytes"],
                    "max_items": limits["max_items"],
                    "max_requests": limits["max_requests"],
                }
                if source_grant:
                    item["source_grant"] = source_grant.snapshot()
                if reading and row["query"] is not None:
                    item["query"] = row["query"]  # Reading v1 only; older wires never see it.
                if row["discovery_mode"] == "recommend":
                    item["discovery_mode"] = "recommend"
                return item
        return None

    def validate_read(
        self, adapter_id, request_id, validation, *, reading=False, current_request=None
    ):
        self.adapter(adapter_id)
        s = self.store
        with nullcontext() if s.in_transaction else s.transaction():
            self._protocol(request_id, reading)
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
                "exploration_requests.request_id = %s AND exploration_requests.adapter_id = %s",
                (request_id, adapter_id),
            )
            if (
                not row
                or row["lease_token"] != validation.lease_token
                or row["state"] != "RUNNING"
                or row["lease_until"] <= self.clock()
            ):
                raise CoreError("STALE_READER_LEASE", 409)
            if row["captured_privacy_epoch"] != epoch(s):
                raise CoreError("PRIVACY_CHANGED", 409)
            if row["discovery_mode"] == "recommend" and (
                self.adapter(adapter_id).bilibili_discovery != "recommend"
            ):
                raise CoreError("DISCOVERY_MODE_CHANGED", 409)
            self._read_scope(row, current_request=current_request)
            captured = self.observations.grants.captured(row)
            if captured != validation.source_grant:
                raise CoreError("SOURCE_GRANT_REVISION_CHANGED", 409)
            if captured or self.adapter(adapter_id).source_grant_required:
                self.observations.grants.require(adapter_id, captured)
            step = json.loads(row["goal_step_json"])
            if step:
                self.goals.validate_lease(step)
        return {"valid": True}

    def _protocol(self, request_id, reading):
        if not reading and self.store.one(
            "SELECT 1 FROM reading_attempts WHERE request_id = %s",
            (request_id,),
        ):
            raise CoreError("READER_PROTOCOL_MISMATCH", 409)

    def complete(self, adapter_id, request_id, result, *, reading=False):
        policy = self.adapter(adapter_id)
        s = self.store
        receipt = result.model_dump(mode="json")
        if receipt["source_grant"] is None:
            receipt.pop("source_grant")
        if receipt["used_requests"] is None:
            receipt.pop("used_requests")
        for item in receipt["items"]:
            for field in ("reading", "article"):
                if item[field] is None:
                    item.pop(field)
        fingerprint = digest(receipt)
        result_ids, failed_step = [], None
        with nullcontext() if s.in_transaction else s.transaction():
            now = self.clock()
            self._protocol(request_id, reading)
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
                "exploration_requests.request_id = %s AND exploration_requests.adapter_id = %s",
                (request_id, adapter_id),
            )
            if not row or row["lease_token"] != result.lease_token:
                raise CoreError("STALE_READER_LEASE", 409)
            if row["state"] in {"DONE", "FAILED", "UNKNOWN"} and row["result_sha256"]:
                if row["result_sha256"] != fingerprint:
                    raise CoreError("IDEMPOTENCY_CONFLICT", 409)
                return {"state": row["state"], "duplicate": True}
            if row["state"] != "RUNNING" or row["lease_until"] <= now:
                raise CoreError("STALE_READER_LEASE", 409)
            if row["captured_privacy_epoch"] != epoch(s):
                raise CoreError("PRIVACY_CHANGED", 409)
            self._read_scope(row)
            captured_grant = self.observations.grants.captured(row)
            if captured_grant != result.source_grant:
                raise CoreError("SOURCE_GRANT_REVISION_CHANGED", 409)
            if captured_grant or policy.source_grant_required:
                self.observations.grants.require(adapter_id, captured_grant)
            step = json.loads(row["goal_step_json"])
            paused = s.one("SELECT value FROM runtime_meta WHERE key = 'life_paused'")
            if paused and paused["value"] == "1" and self.autonomous(row, step):
                raise CoreError("AUTONOMY_PAUSED", 409)
            if step:
                self.goals.validate_lease(step)
            if result.used_bytes is not None and result.used_bytes > row["reserved_bytes"]:
                raise CoreError("READER_BYTE_LIMIT", 400)
            used_requests = result.used_requests
            if used_requests is None and row["reserved_requests"] == 1:
                used_requests = 1
            if used_requests is not None and used_requests > row["reserved_requests"]:
                raise CoreError("READER_REQUEST_LIMIT", 400)
            if result.status == "SUCCESS":
                if not used_requests:
                    raise CoreError("READER_USAGE_REQUIRED", 400)
                sources = [
                    source["event_id"]
                    for source in s.rows(
                        "SELECT event_id FROM exploration_evidence WHERE request_id = %s",
                        (request_id,),
                    )
                ]
                if result.used_bytes is None or result.used_bytes == 0:
                    raise CoreError("READER_USAGE_REQUIRED", 400)
                if row["operation"] == "read" and len(result.items) != 1:
                    raise CoreError("READ_REQUIRES_ONE_RESULT", 400)
                day, _ = self.periods()
                count = s.one(
                    "SELECT COUNT(*) AS n FROM observations AS o JOIN events AS e USING (event_id) "
                    'WHERE e.source IN (SELECT value COLLATE "C" FROM '
                    "JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS parsed(value)) AND "
                    "result_status = 'DISCOVERED' AND o.created_at >= %s",
                    (encode(observation_sources("discover")), day),
                )["n"]
                propose_goals = self.settings.goals_enabled
                idle_discovery = row["operation"] == "discover" and s.one(
                    "SELECT 1 FROM wake_decisions WHERE decision_id = %s AND reason_code = %s",
                    (row["request_key"], IDLE_DISCOVERY),
                )
                for index, item in enumerate(result.items):
                    source, canonical = content_target(
                        item.url,
                        web_hosts=self.settings.web_reading_hosts if reading else (),
                    )
                    if source != policy.source or (
                        row["url"] and canonical != row["url"] and not (reading and source == "web")
                    ):
                        raise CoreError("READER_TARGET_MISMATCH", 400)
                    discovered = row["operation"] == "discover"
                    if discovered and count >= self.settings.exploration_daily_candidate_limit:
                        break
                    text = item.title if discovered else item.text.strip()
                    if not text:
                        raise CoreError("READ_BODY_REQUIRED", 400)
                    if item.article and (
                        item.image_urls != [image.url for image in item.article.images]
                        or item.article.response_bytes > result.used_bytes
                    ):
                        raise CoreError("ARTICLE_EVIDENCE_MISMATCH", 400)
                    provider_id = request_id + ":" + str(index)
                    observation = ObservationEvent(
                        schema_version=2,
                        event_id=uid("observation"),
                        event_type="content.observed",
                        source=source,
                        source_kind="title" if discovered else "text",
                        provider_event_id=provider_id,
                        occurred_at=now,
                        trace_id=request_id,
                        result_status="DISCOVERED" if discovered else "FETCHED",
                        source_event_ids=sources,
                        source_sha256=hashlib.sha256(text.encode()).hexdigest(),
                        privacy_epoch=epoch(s),
                        source_grant=captured_grant,
                        payload=ObservationPayload(
                            title=item.title,
                            text=text,
                            url=canonical,
                            requested_url=json.loads(step["scope_json"])["url"]
                            if step
                            else row["url"]
                            if reading
                            else "",
                            reading=item.reading,
                            article=item.article,
                        ),
                    )
                    event_id = self.observations.append(
                        adapter_id,
                        observation,
                        reading=reading,
                    )["event_id"]
                    s.insert("exploration_evidence", request_id=request_id, event_id=event_id)
                    result_ids.append(event_id)
                    if not discovered and item.image_urls and self.settings.content_enabled:
                        for source_id in sources:
                            if s.one(
                                "SELECT 1 FROM events WHERE event_id = %s AND actor_id = 'owner' "
                                "AND source = 'qq.onebot'",
                                (source_id,),
                            ):
                                from companion.content import register_link_images

                                register_link_images(
                                    s, self.settings, self.clock,
                                    self.content_options or self.settings.content,
                                    source_id, event_id, item.image_urls
                                )
                    if discovered:
                        count += 1
                        existing = s.one(
                            (
                                (
                                    'SELECT 1 FROM goals WHERE redacted=0 AND (CASE WHEN '
                                    'scope_json IS JSON THEN '
                                    "(scope_json::json#>>'{url}') END)=%s"
                                )
                            ),
                            (canonical,),
                        ) or s.one(
                            # Already watched: the ledger keeps one entry per item.
                            "SELECT 1 FROM watch_ledger WHERE item_ref = %s",
                            (canonical,),
                        )
                        if not existing and propose_goals:
                            try:
                                self.goals.propose(
                                    GoalProposal(
                                        topic=item.topic or item.title[:80],
                                        source_event_ids=[event_id],
                                        scope=GoalScope(operation="read_content", url=canonical),
                                    )
                                )
                                if idle_discovery:
                                    propose_goals = False  # One reading goal per idle listing.
                            except CoreError as error:
                                if error.code != "GOAL_CAPACITY_REACHED":
                                    raise
                    elif step:
                        self.goals.finish(step, event_id)
                state = "DONE"
            else:
                if result.items:
                    raise CoreError("FAILED_READ_HAS_CONTENT", 400)
                state = result.status
                failed_step = step or None
            s.execute(
                "UPDATE exploration_requests SET state = %s, used_bytes = %s, used_requests = %s, "
                "result_sha256 = %s, last_error_code = %s, updated_at = %s WHERE request_id = %s",
                (
                    state,
                    None if state == "UNKNOWN" else result.used_bytes,
                    None if state == "UNKNOWN" else used_requests,
                    fingerprint,
                    result.error_code,
                    now,
                    request_id,
                ),
            )
        if failed_step and not reading:
            self.goals.fail(
                failed_step,
                result.error_code or "READ_FAILED",
                execution_uncertain=state == "UNKNOWN",
            )
        if step and step["kind"] == "self" and not reading:
            for event_id in result_ids:
                self.social.consider(event_id)
        return {"state": state, "event_ids": result_ids, "duplicate": False}
