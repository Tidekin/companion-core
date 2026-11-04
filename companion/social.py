"""One owner-only sharing path, with common proactive limits and source erasure."""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from companion import owner_authority
from companion.contracts import CoreError
from companion.observation import expired_sources
from companion.privacy import epoch, redact, sensitive
from companion.sources import content_target, observation_sources, source_definition, supports
from companion.store import after, uid, utcnow


def quiet_hours(settings, now):
    hour = datetime.fromisoformat(now).astimezone(ZoneInfo(settings.timezone)).hour
    start, end = settings.quiet_start, settings.quiet_end
    return start != end and (start <= hour < end if start < end else hour >= start or hour < end)


def proactive_allowed(store, settings, now):
    if store.one(
        "SELECT 1 FROM runtime_meta WHERE key IN ('proactive_paused','life_paused') AND value='1'"
    ):
        return False
    if settings is None:
        return True
    if not settings.proactive_enabled or quiet_hours(settings, now):
        return False
    return not feedback_paused(store, settings, now)


def feedback_paused(store, settings, now, source_ids=()):
    """Feedback can regulate autonomy only under its independent current purpose."""
    if not settings.affect_enabled:
        return False
    if owner_authority.required(store, settings):
        try:
            view = owner_authority.current(store, settings, lambda: now)
        except CoreError:
            return True
        if "affect" not in view.policy.purposes:
            return False
    from companion.affect import AffectEngine

    return AffectEngine(store, settings, lambda: now).contact_paused(source_ids)


def proactive_counts(store, settings, now):
    day = (
        datetime.fromisoformat(now)
        .astimezone(ZoneInfo(settings.timezone))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
        .isoformat(timespec="microseconds")
    )
    recent = after(now, -settings.proactive_interval_seconds)
    # Ordinary activity reserves before wording. Life SHADOW choices reserve no
    # send: count their actual native Outbox group once at its enqueue time.
    row = store.one(
        "WITH reservations(created_at) AS ("
        "SELECT j.created_at FROM jobs j JOIN events e USING(event_id) "
        "WHERE j.kind='activity' AND COALESCE(json_extract(e.body_json,'$.notice.kind'),'') "
        "NOT IN ('commitment','life','followup') AND NOT (j.state='SUCCEEDED' AND EXISTS "
        "(SELECT 1 FROM social_candidates c WHERE c.candidate_id=json_extract(e.body_json,"
        "'$.notice.ref') AND c.state='SKIPPED' AND c.reason_code='MODEL_SKIPPED') AND NOT EXISTS "
        "(SELECT 1 FROM outbox o WHERE o.origin_event_id=j.event_id)) UNION ALL "
        "SELECT updated_at FROM social_candidates WHERE message_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM outbox o JOIN jobs j ON j.event_id=o.origin_event_id "
        "WHERE o.message_id=social_candidates.message_id AND j.kind='activity') UNION ALL "
        "SELECT MIN(o.created_at) FROM outbox o JOIN jobs j ON j.event_id=o.origin_event_id "
        "JOIN events e ON e.event_id=j.event_id WHERE j.kind='activity' "
        "AND json_extract(e.body_json,'$.notice.kind')='life' "
        "AND o.action_type='qq.private.proactive' GROUP BY o.origin_event_id) "
        "SELECT COALESCE(SUM(created_at>=?),0) n,MAX(created_at)>? recent "
        "FROM reservations WHERE created_at>=?", (day, recent, min(day, recent)),
    )
    return row["n"], bool(row["recent"])



def text_key(text):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text.casefold())


def semantic_duplicate(text, candidates, settings):
    if not candidates:
        return False
    import numpy as np

    from companion.retrieval import encoder

    model = encoder(str(Path(settings.embedding_cache_dir).resolve()))
    vectors = list(model.passage_embed([text] + candidates, batch_size=32))
    return any(
        float(np.dot(vectors[0], vector)) >= settings.social_semantic_threshold
        for vector in vectors[1:]
    )


def _share_url(value, source):
    if not isinstance(value, str):
        return None
    if not value:
        return ""
    try:
        platform, canonical = content_target(value)
        return canonical if platform == source else None
    except CoreError:
        return None


def share_source(event):
    """Accepted historical text stays usable; revoked future reads do not erase it.

    Recheck stored attribution before queueing or sending, without fetching or
    requiring a still-live grant. Only explicit erasure invalidates that history.
    """
    try:
        if (
            event["source"] not in observation_sources("share")
            or event["source_kind"] != "text"
            or event["result_status"] != "FETCHED"
        ):
            return None
        definition = source_definition(event["source"].removeprefix("observation."))
        body = json.loads(event["body_json"])
        if (
            not isinstance(body["title"], str)
            or not isinstance(body["text"], str)
            or not body["text"].strip()
            or hashlib.sha256(body["text"].encode()).hexdigest() != event["source_sha256"]
        ):
            return None
        canonical = _share_url(event["canonical_url"], definition.name)
        if canonical is None:
            return None
        evidence = json.loads(event["evidence_json"])
        if any(
            evidence.get(kind) is not None and not supports(definition.name, kind)
            for kind in ("reading", "article")
        ):
            return None
        return definition, body, canonical
    except (AttributeError, CoreError, KeyError, TypeError, ValueError):
        return None


def share_delivery_problem(store, settings, message_id, now):
    """Recheck native queued shares under the transport's write transaction."""
    from companion import notices

    try:
        job = notices.delivery_job(store, message_id)
    except (CoreError, KeyError, TypeError, ValueError):
        return "NOTICE_SOURCE_CHANGED"
    if job is not None:
        return notices.delivery_problem(store, settings, message_id, now, job=job)
    candidate = store.one(
        "SELECT * FROM social_candidates WHERE message_id=?", (message_id,)
    )
    if not candidate:
        return None  # Ordinary replies and other proactive work retain their own gates.
    if settings is None or not settings.social_enabled:
        return "SOCIAL_DISABLED"
    if candidate["state"] != "QUEUED" or candidate["expires_at"] <= now:
        return "SOCIAL_CANDIDATE_EXPIRED"
    cutoff = after(now, -86400 * settings.raw_event_retention_days)
    if expired_sources(store, [candidate["event_id"]], cutoff):
        return "SOCIAL_SOURCE_EXPIRED"
    event = store.one(
        "SELECT o.*,e.body_json,e.source FROM observations o JOIN events e USING(event_id) "
        "WHERE o.event_id=? AND o.redacted=0 AND e.redacted=0 AND e.privacy_scope='private' "
        "AND e.occurred_at<=?", (candidate["event_id"], now)
    )
    resolved = share_source(event) if event else None
    if resolved is None:
        return "SOCIAL_SOURCE_CHANGED"
    definition, body, canonical = resolved
    comparison = body["title"] + "\n" + body["text"][:1500]
    if (
        canonical != _share_url(candidate["canonical_url"], definition.name)
        or hashlib.sha256(text_key(comparison).encode()).hexdigest()
        != candidate["content_sha256"]
    ):
        return "SOCIAL_SOURCE_CHANGED"
    delivery = store.one(
        "SELECT o.content_json,o.action_type,o.origin_event_id,c.scope,c.owner_actor_id "
        "FROM outbox o JOIN conversations c USING(conversation_id) WHERE o.message_id=?",
        (message_id,),
    )
    if (
        not delivery or delivery["action_type"] != "qq.private.proactive"
        or delivery["origin_event_id"] != candidate["event_id"]
        or delivery["scope"] != "private" or delivery["owner_actor_id"] != "owner"
    ):
        return "SOCIAL_DELIVERY_CHANGED"
    try:
        return "SOCIAL_WORDING_REQUIRED"
    except (KeyError, TypeError, ValueError):
        return "SOCIAL_CONTENT_CHANGED"
    return None


class SocialPlanner:
    def __init__(self, store, settings, clock=utcnow):
        self.store, self.settings, self.clock = store, settings, clock

    def preview(self, event_id, requester):
        """An owner-only shadow draft. Does not create a candidate or reserve a send."""
        if requester != self.settings.owner_qq_id:
            raise CoreError("OWNER_REQUIRED", 403)
        s, now = self.store, self.clock()
        captured = epoch(s)
        cutoff = after(now, -86400 * self.settings.raw_event_retention_days)
        event = s.one(
            "SELECT o.*,e.body_json,e.source,e.ingested_at FROM observations o "
            "JOIN events e USING(event_id) WHERE o.event_id=? AND o.redacted=0 AND e.redacted=0 "
            "AND e.privacy_scope='private' AND e.ingested_at>=? AND e.occurred_at<=? "
            "AND length(e.body_json)<=16000",
            (event_id, cutoff, now),
        )
        if not event or expired_sources(s, [event_id], cutoff):
            return None
        resolved = share_source(event)
        if resolved is None:
            return None
        definition, body, canonical = resolved
        if sensitive(body["title"] + body["text"]) or any(
            redact(body[key], self.settings) != body[key] for key in ("title", "text")
        ):
            return None
        comparison = body["title"] + "\n" + body["text"][:1500]
        key = hashlib.sha256(text_key(comparison).encode()).hexdigest()
        duplicate = s.one(
            "SELECT 1 FROM social_candidates WHERE state<>'ERASED' "
            "AND (canonical_url=? OR content_sha256=?)",
            (canonical, key),
        )
        reason = "DUPLICATE_CONTENT" if duplicate else "SHADOW_ONLY"
        if not proactive_allowed(s, self.settings, now):
            reason = "OUTBOUND_DISABLED_OR_PAUSED"
        if captured != epoch(s):
            raise CoreError("PRIVACY_CHANGED", 409)
        return {
            "mode": "SHADOW",
            "privacy_epoch": captured,
            "event_id": event_id,
            "evidence_level": "FETCHED",
            "source": definition.name,
            "title": body["title"],
            "excerpt": body["text"][:500],
            "url": canonical,
            "expires_at": after(
                event["ingested_at"], 86400 * self.settings.raw_event_retention_days
            ),
            "reason_code": reason,
            "duplicate": bool(duplicate),
            "requires_semantic_review": True,
            "auto_share": False,
        }

    def consider(self, event_id):
        from companion.wake import IDLE_READ, IDLE_READ_RESULTS

        if not self.settings.social_enabled or self.settings.life_loop.mode != "OFF":
            return None
        s, now, captured = self.store, self.clock(), epoch(self.store)
        if expired_sources(
            s, [event_id], after(now, -86400 * self.settings.raw_event_retention_days)
        ):
            return None
        event = s.one(
            "SELECT o.*,e.body_json,e.source FROM observations o JOIN events e USING(event_id) "
            "WHERE o.event_id=? AND o.redacted=0 AND e.redacted=0 AND o.result_status='FETCHED' "
            "AND e.source IN (SELECT value FROM json_each(?)) "
            f"AND o.event_id NOT IN ({IDLE_READ_RESULTS})",
            (event_id, json.dumps(observation_sources("share")), IDLE_READ),
        )
        if not event:
            return None
        resolved = share_source(event)
        if resolved is None:
            return None
        _, body, canonical = resolved
        existing = s.one("SELECT candidate_id FROM social_candidates WHERE event_id=?", (event_id,))
        if existing:
            return existing["candidate_id"]
        comparison = body["title"] + "\n" + body["text"][:1500]
        normalized = text_key(comparison)
        key = hashlib.sha256(normalized.encode()).hexdigest()
        candidate_id = uid("share")
        for attempt in range(3):
            generation = s.one("SELECT COALESCE(MAX(rowid),0) AS n FROM social_candidates")["n"]
            recent = s.rows(
                "SELECT * FROM social_candidates WHERE state<>'ERASED' AND created_at>=? "
                "ORDER BY rowid DESC LIMIT 100",
                (after(now, -30 * 86400),),
            )
            reason = None
            if s.one(
                "SELECT 1 FROM social_candidates WHERE state<>'ERASED' "
                "AND (canonical_url=? OR content_sha256=?)",
                (canonical, key),
            ):
                reason = "DUPLICATE_CONTENT"
            elif recent:
                try:
                    if semantic_duplicate(
                        comparison, [row["comparison_text"] for row in recent], self.settings
                    ):
                        reason = "SIMILAR_CONTENT"
                except Exception:
                    reason = "DEDUPE_UNAVAILABLE"
            with s.transaction():
                if captured != epoch(s):
                    return None
                if expired_sources(
                    s, [event_id],
                    after(self.clock(), -86400 * self.settings.raw_event_retention_days),
                ):
                    return None
                if s.one("SELECT 1 FROM social_candidates WHERE event_id=?", (event_id,)):
                    return None
                latest = s.one("SELECT COALESCE(MAX(rowid),0) AS n FROM social_candidates")["n"]
                if latest != generation:
                    # Recompute outside the write transaction against concurrently added content.
                    if attempt < 2:
                        continue
                    reason = "DEDUPE_CONTENDED"
                s.insert(
                    "social_candidates",
                    candidate_id=candidate_id,
                    event_id=event_id,
                    canonical_url=canonical,
                    content_sha256=key,
                    comparison_text=comparison,
                    state="SKIPPED" if reason else "CANDIDATE",
                    reason_code=reason,
                    expires_at=after(now, 86400),
                    created_at=now,
                    updated_at=now,
                )
            return candidate_id

    def flush(self):
        cfg, s, now = self.settings, self.store, self.clock()
        if not cfg.social_enabled or not cfg.pricing_known or cfg.life_loop.mode != "OFF":
            return False
        with s.transaction():
            if not proactive_allowed(s, cfg, now):
                return False
            s.execute(
                "UPDATE social_candidates SET state='EXPIRED' WHERE state='CANDIDATE' "
                "AND expires_at<=?",
                (now,),
            )
            conversation = s.one(
                "SELECT * FROM conversations WHERE platform='qq.onebot' AND scope='private' "
                "AND owner_actor_id='owner' ORDER BY last_event_at DESC LIMIT 1"
            )
            if not conversation or conversation["last_event_at"] > after(
                now, -cfg.proactive_idle_seconds
            ):
                return False
            if s.one("SELECT 1 FROM jobs WHERE state IN ('QUEUED','RUNNING','RETRY_WAIT')"):
                return False
            if s.one(
                "SELECT 1 FROM outbox WHERE state IN "
                "('PENDING','LEASED','SENDING','RETRY_WAIT','DELIVERY_UNCERTAIN')"
            ):
                return False
            count, recent = proactive_counts(s, cfg, now)
            if recent or count >= cfg.proactive_daily_limit:
                return False
            candidates = s.rows(
                "SELECT c.*,e.body_json,e.source,o.source_kind,o.result_status,"
                "o.source_sha256,o.evidence_json,o.canonical_url AS source_url "
                "FROM social_candidates c JOIN events e USING(event_id) "
                "JOIN observations o USING(event_id) WHERE c.state='CANDIDATE' "
                "AND e.redacted=0 AND o.redacted=0 ORDER BY c.created_at,c.candidate_id LIMIT 32"
            )
            candidate = next((item for item in candidates if not feedback_paused(
                s, cfg, now, [item["event_id"]])), None)
            if not candidate:
                return False
            if expired_sources(
                s, [candidate["event_id"]], after(now, -86400 * cfg.raw_event_retention_days)
            ):
                s.execute(
                    "UPDATE social_candidates SET state='EXPIRED',reason_code='SOURCE_EXPIRED',"
                    "updated_at=? WHERE candidate_id=?", (now, candidate["candidate_id"])
                )
                return False
            resolved = share_source({**candidate, "canonical_url": candidate["source_url"]})
            if resolved is None or resolved[2] != _share_url(
                candidate["canonical_url"], resolved[0].name
            ):
                s.execute(
                    "UPDATE social_candidates SET state='SKIPPED',reason_code='SOURCE_CHANGED',"
                    "updated_at=? WHERE candidate_id=?",
                    (now, candidate["candidate_id"]),
                )
                return False
            from companion.notices import schedule

            schedule(s, cfg, "share", candidate["candidate_id"],
                     conversation["conversation_id"], now)
            s.execute(
                "UPDATE social_candidates SET state='QUEUED',updated_at=? "
                "WHERE candidate_id=?",
                (now, candidate["candidate_id"]),
            )
        return True
