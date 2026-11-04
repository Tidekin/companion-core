"""Persistent, low-cost wake decisions; produces bounded read intentions only."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from companion import delegation, owner_authority
from companion.contracts import CoreError
from companion.goals import AUTHORITY_REJECTIONS
from companion.store import after, encode, uid

WAKE_PURPOSES = ("wake", "goals", "exploration")


class WakePlanner:
    def __init__(self, store, settings, goals, affect, clock):
        self.store, self.settings, self.goals, self.affect, self.clock = (
            store,
            settings,
            goals,
            affect,
            clock,
        )

    def evaluate(self, trigger_event_id=None):
        cfg, s, now = self.settings, self.store, self.clock()
        if not cfg.wake_enabled or not cfg.goals_enabled:
            return []
        if cfg.life_loop.mode != "OFF" and cfg.life_loop.read_goals_enabled:
            return []  # Life decisions select new self goals; accepted native steps still run.
        view = None
        if owner_authority.required(s, cfg):
            try:
                view = owner_authority.current(s, cfg, self.clock, purpose="wake")
            except CoreError:
                return []
            if not set(WAKE_PURPOSES) <= set(view.policy.purposes):
                return []
        self.goals.expire_sources()
        local = datetime.fromisoformat(now).astimezone(ZoneInfo(cfg.timezone))
        # A delegated companion adjustment can only lengthen the interval or lower the count.
        interval = delegation.effective(s, cfg, "wake", "wake_min_interval_minutes", now)
        max_runs = delegation.effective(s, cfg, "wake", "wake_max_daily_runs", now)
        slot = int(local.timestamp()) // (interval * 60)
        key = "event:" + trigger_event_id if trigger_event_id else "slot:" + str(slot)
        with s.transaction():
            existing = s.one("SELECT * FROM wake_decisions WHERE idempotency_key=?", (key,))
            if existing:
                return []
            if trigger_event_id:
                self.goals.validate_sources([trigger_event_id])
            day = (
                local.replace(hour=0, minute=0, second=0, microsecond=0)
                .astimezone(UTC)
                .isoformat(timespec="microseconds")
            )
            count = s.one(
                "SELECT COUNT(*) AS n FROM wake_decisions WHERE decision<>'SKIP' AND created_at>=?",
                (day,),
            )["n"]
            recent = s.one(
                "SELECT 1 FROM wake_decisions WHERE decision<>'SKIP' AND created_at>?",
                (after(now, -interval * 60),),
            )
            quiet = cfg.quiet_start != cfg.quiet_end and (
                cfg.quiet_start <= local.hour < cfg.quiet_end
                if cfg.quiet_start < cfg.quiet_end
                else local.hour >= cfg.quiet_start or local.hour < cfg.quiet_end
            )
            paused = s.one("SELECT value FROM runtime_meta WHERE key='life_paused'")
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
                for candidate in s.rows(
                    "SELECT * FROM goals WHERE kind='self' AND redacted=0 AND state IN "
                    "('CANDIDATE','ACCEPTED','WAITING_RESOURCE','RETRY_WAIT') AND "
                    "next_due_at<=? ORDER BY priority DESC,created_at LIMIT 64",
                    (now,),
                ):
                    try:
                        accepted = self.goals.accept(candidate["goal_id"], autonomous=True)
                    except CoreError as error:
                        # A goal proposed under an earlier grant must not block every later slot.
                        if error.code not in AUTHORITY_REJECTIONS:
                            raise
                        self.goals.reject_authority(candidate["goal_id"], error.code)
                        continue
                    if accepted["state"] == "QUEUED":
                        reason = "GOAL_DUE"
                    break
            goal_id = candidate["goal_id"] if reason == "GOAL_DUE" else None
            if reason == "NO_CANDIDATE" and cfg.exploration_daily_requests > 0:
                reason = "DISCOVERY_DUE"
            decision = "EXPLORE" if goal_id else "OBSERVE" if reason == "DISCOVERY_DUE" else "SKIP"
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
                [{"decision_id": decision_id, "decision": decision, "goal_id": goal_id}]
                if decision != "SKIP"
                else []
            )
