"""Persistent, low-cost wake decisions; produces bounded read intentions only."""

from contextlib import nullcontext
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from companion import budget, delegation, owner_authority, topic_selection
from companion.contracts import CoreError
from companion.goals import AUTHORITY_REJECTIONS
from companion.store import after, encode, uid

WAKE_PURPOSES = ("wake", "goals", "exploration")
IDLE_DISCOVERY = "IDLE_BILIBILI_DISCOVERY"
IDLE_READ = "IDLE_BILIBILI_READ"
IDLE_READ_RESULTS = (
    "SELECT g.result_event_id FROM wake_decisions w JOIN "
    "goals g USING(goal_id) WHERE w.reason_code=%s AND "
    "g.state='DONE' AND g.redacted=0 AND g.result_event_id "
    "IS NOT NULL"
)


class WakePlanner:
    def __init__(self, store, settings, goals, affect, clock):
        self.store, self.settings, self.goals, self.affect, self.clock = (
            store,
            settings,
            goals,
            affect,
            clock,
        )

    def _idle_reads(self, now):
        # Only native single-item goals backed by the exact public discovery receipt.
        rows = self.store.rows(
            (
                (
                    'SELECT g.*,r.query FROM goals g JOIN goal_evidence x '
                    'USING(goal_id) JOIN observations o ON '
                    'o.event_id=x.event_id JOIN events e ON '
                    'e.event_id=o.event_id JOIN exploration_requests r ON '
                    "r.request_id=e.trace_id WHERE g.kind='self' AND "
                    "g.state='CANDIDATE' AND g.redacted=0 AND "
                    'g.next_due_at<=%s AND g.reflection_suggestion_id IS '
                    "NULL AND e.source='observation.bilibili' AND "
                    "o.result_status='DISCOVERED' AND e.redacted=0 AND "
                    "o.redacted=0 AND r.operation='discover' AND "
                    "r.state='DONE' AND r.redacted=0 AND (CASE WHEN "
                    'g.scope_json IS JSON THEN '
                    "(g.scope_json::json#>>'{operation}') "
                    "END)='read_content' AND (CASE WHEN g.scope_json IS "
                    "JSON THEN (g.scope_json::json#>>'{url}') "
                    'END)=o.canonical_url AND NOT EXISTS (SELECT 1 FROM '
                    'goal_evidence x2 WHERE x2.goal_id=g.goal_id AND '
                    'x2.event_id<>x.event_id) AND NOT EXISTS (SELECT 1 FROM '
                    'life_activity_results a WHERE a.goal_id=g.goal_id) '
                    'ORDER BY g.priority DESC,g.created_at DESC,'
                    'g.record_sequence LIMIT 64'
                )
            ),
            (now,),
        )
        interests = topic_selection.interest_topics(self.store, now) if rows else []
        return sorted(
            rows,
            key=lambda row: sum(
                weight for term, weight in interests if term == row["query"] or term in row["topic"]
            ),
            reverse=True,
        )

    def evaluate(self, trigger_event_id=None):
        cfg, s, now = self.settings, self.store, self.clock()
        if not cfg.wake_enabled or not cfg.goals_enabled:
            return []
        idle = cfg.life_loop.mode != "OFF" and cfg.life_loop.read_goals_enabled
        if idle and (
            cfg.life_loop.mode != "SHADOW"
            or not cfg.life_loop.autonomy_enabled
            or trigger_event_id
            or not cfg.observation_enabled
            or not any(a.source == "bilibili" for a in cfg.observation_adapters.values())
        ):
            return []
        view = None
        if owner_authority.required(s, cfg):
            try:
                view = owner_authority.current(s, cfg, self.clock, purpose="wake")
            except CoreError:
                return []
            if not set(WAKE_PURPOSES) <= set(view.policy.purposes):
                return []
            if idle and not {"life", "content"} <= set(view.policy.purposes):
                return []
        if not idle:
            self.goals.expire_sources()
        local = datetime.fromisoformat(now).astimezone(ZoneInfo(cfg.timezone))
        # A delegated companion adjustment can only lengthen the interval or lower the count.
        interval = delegation.effective(s, cfg, "wake", "wake_min_interval_minutes", now)
        max_runs = delegation.effective(s, cfg, "wake", "wake_max_daily_runs", now)
        slot = int(local.timestamp()) // (interval * 60)
        key = "event:" + trigger_event_id if trigger_event_id else "slot:" + str(slot)
        with nullcontext() if s.in_transaction else s.transaction():
            if s.one("SELECT 1 FROM wake_decisions WHERE idempotency_key = %s", (key,)):
                return []
            if idle and (
                budget.chat_pending(s, now)
                or s.one(
                    (
                        'SELECT 1 FROM conversations WHERE platform = '
                        "'qq.onebot' AND scope = 'private' AND owner_actor_id = "
                        "'owner' AND last_event_at > %s LIMIT 1"
                    ),
                    (after(now, -cfg.proactive_idle_seconds),),
                )
                or s.one(
                    (
                        "SELECT 1 FROM life_topics WHERE state IN ('WAITING', "
                        "'DUE') AND expires_at > %s AND reason_code <> "
                        "'WAITING_FOR_NEW_EVIDENCE' LIMIT 1"
                    ),
                    (now,),
                )
            ):
                return []
            if trigger_event_id:
                self.goals.validate_sources([trigger_event_id])
            day = (
                local.replace(hour=0, minute=0, second=0, microsecond=0)
                .astimezone(UTC)
                .isoformat(timespec="microseconds")
            )
            count = s.one(
                "SELECT COUNT(*) AS n FROM wake_decisions WHERE decision <> 'SKIP' AND created_at "
                ">= %s",
                (day,),
            )["n"]
            recent = s.one(
                "SELECT 1 FROM wake_decisions WHERE decision <> 'SKIP' AND created_at > %s",
                (after(now, -interval * 60),),
            )
            quiet = cfg.quiet_start != cfg.quiet_end and (
                cfg.quiet_start <= local.hour < cfg.quiet_end
                if cfg.quiet_start < cfg.quiet_end
                else local.hour >= cfg.quiet_start or local.hour < cfg.quiet_end
            )
            paused = s.one("SELECT value FROM runtime_meta WHERE key = 'life_paused'")
            reason, candidate = "NO_CANDIDATE", None
            if paused and paused["value"] == "1":
                reason = "PAUSED"
            elif delegation.paused(s, "wake", now):
                reason = "COMPANION_PAUSED"
            elif quiet:
                reason = "QUIET_HOURS"
            elif not cfg.pricing_known or not cfg.exploration_enabled:
                reason = "RESOURCE_UNAVAILABLE"
            elif count >= max_runs:
                reason = "DAILY_LIMIT"
            elif recent:
                reason = "COOLDOWN"
            else:
                if idle:
                    self.goals.expire_sources()
                candidates = (
                    self._idle_reads(now)
                    if idle
                    else s.rows(
                        (
                            'SELECT goals.goal_id AS goal_id, goals.kind AS kind, '
                            'goals.topic AS topic, goals.scope_json AS scope_json, '
                            'goals.proposal_key AS proposal_key, goals.state AS '
                            'state, goals.priority AS priority, goals.next_due_at '
                            'AS next_due_at, goals.last_error_code AS '
                            'last_error_code, goals.version AS version, '
                            'goals.result_event_id AS result_event_id, '
                            'goals.created_at AS created_at, goals.updated_at AS '
                            'updated_at, goals.redacted AS redacted, '
                            'goals.reflection_suggestion_id AS '
                            'reflection_suggestion_id, goals.authority_json AS '
                            'authority_json FROM goals AS goals WHERE goals.kind = '
                            "'self' AND goals.redacted = 0 AND goals.state IN "
                            "('CANDIDATE', 'ACCEPTED', 'WAITING_RESOURCE', "
                            "'RETRY_WAIT') AND goals.next_due_at <= %s ORDER BY "
                            'goals.priority DESC NULLS LAST, goals.created_at NULLS '
                            'FIRST LIMIT 64'
                        ),
                        (now,),
                    )
                )
                for candidate in candidates:
                    try:
                        accepted = self.goals.accept(candidate["goal_id"], autonomous=True)
                    except CoreError as error:
                        # A goal proposed under an earlier grant must not block every later slot.
                        if error.code not in AUTHORITY_REJECTIONS:
                            raise
                        self.goals.reject_authority(candidate["goal_id"], error.code)
                        continue
                    if accepted["state"] == "QUEUED":
                        reason = IDLE_READ if idle else "GOAL_DUE"
                    break
            goal_id = candidate["goal_id"] if reason in ("GOAL_DUE", IDLE_READ) else None
            if reason == "NO_CANDIDATE" and cfg.exploration_daily_requests > 0:
                reason = IDLE_DISCOVERY if idle else "DISCOVERY_DUE"
            decision = (
                "EXPLORE"
                if goal_id
                else "OBSERVE"
                if reason in ("DISCOVERY_DUE", IDLE_DISCOVERY)
                else "SKIP"
            )
            decision_id = uid("wake")
            s.insert(
                "wake_decisions",
                decision_id=decision_id,
                idempotency_key=key,
                trigger_event_id=trigger_event_id,
                goal_id=goal_id,
                decision=decision,
                reason_code=reason,
                budget_snapshot=encode({"daily_runs": count, "limit": max_runs}),
                created_at=now,
            )
            return (
                [
                    {
                        "decision_id": decision_id,
                        "decision": decision,
                        "goal_id": goal_id,
                        "reason_code": reason,
                    }
                ]
                if decision != "SKIP"
                else []
            )
