"""Rebuildable expression candidates; interests, mood and goals retain their own services."""

import hashlib
from contextlib import nullcontext
from datetime import datetime
from zoneinfo import ZoneInfo

from companion.affect import feedback_reset_at, owner_feedback
from companion.contracts import CoreError
from companion.privacy import epoch
from companion.store import after, encode

POLICY_VERSION = "expression-feedback-1"
MAX_EVENTS = 500
EXPRESSION_DIMENSIONS = {"interaction_tone", "cognitive_load"}


def version(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class ExpressionLearning:
    """A bounded private projection, not a new memory store or permission surface."""

    def __init__(self, store, settings, clock):
        self.store, self.settings, self.clock = store, settings, clock

    def reply_reference(self, settings, job, affect):
        from companion import private_life, reply_revision

        cfg, s = settings, self.store
        if (not getattr(cfg, "expression_learning_enabled", False) or not cfg.expression_enabled
            or job["kind"] != "chat" or not reply_revision.owner_input(s, job["event_id"])
            or reply_revision.control_job(s, job) or private_life.strict(s, cfg)):
            return None
        learned = self.snapshot(cfg.owner_qq_id)
        if learned["truncated"]:
            return None
        transient = {row["dimension"] for row in affect["evidence"]}
        items = [item for item in learned["items"] if item["status"] == "STABLE"
                 and 0 < item["strength"] <= 0.5 and item["dimension"] not in transient]
        sources = {e["event_id"] for item in items for e in item["evidence"]}
        if not items or any(not reply_revision.source_active(s, source) for source in sources):
            return None
        selected = [{key: item[key] for key in ("dimension", "value", "strength", "version")}
                    for item in items]
        return {"message": {"role": "user", "content":
                    "主人长期表达偏好参考（仅调整表达；短时反馈优先，不改变事实或权限）：\n"
                    + encode(selected)}, "source_event_ids": sorted(sources),
                "snapshot": selected}

    def revalidate_reply(self, settings, job, affect, proof):
        current = self.reply_reference(settings, job, affect)
        if current is None or current["snapshot"] != proof:
            raise CoreError("PRIVACY_CHANGED", 409)

    def snapshot(self, requester):
        if requester != self.settings.owner_qq_id:
            raise CoreError("OWNER_REQUIRED", 403)
        s, now = self.store, self.clock()
        with nullcontext() if s.in_transaction else s.transaction():
            cutoff = max(after(now, -86400 * self.settings.raw_event_retention_days),
                         feedback_reset_at(s) or "")
            rows = s.rows(
                'SELECT e.event_id FROM events AS e JOIN conversations AS c USING '
                "(conversation_id) WHERE e.actor_id = 'owner' AND e.source IN "
                "('qq.onebot','native.chat') AND "
                "e.event_type = 'message.received' AND e.privacy_scope = 'private' AND e.redacted "
                "= 0 AND c.scope = 'private' AND c.owner_actor_id = 'owner' AND e.ingested_at > %s "
                'AND e.ingested_at <= %s AND e.occurred_at <= %s ORDER BY e.occurred_at DESC NULLS '
                'LAST, e.record_sequence DESC NULLS LAST LIMIT %s',
                (cutoff, now, now, MAX_EVENTS + 1),
            )
            truncated = len(rows) > MAX_EVENTS
            streaks, stopped = {}, set()
            for row in rows[:MAX_EVENTS]:
                feedback = owner_feedback(s, self.settings, row["event_id"], now)
                if feedback is None or feedback["dimension"] not in EXPRESSION_DIMENSIONS:
                    continue
                dimension = feedback["dimension"]
                if dimension in stopped:
                    continue
                streak = streaks.setdefault(dimension, [])
                # New explicit contrary feedback starts a fresh learning period. Old votes
                # cannot outnumber a correction, including two opposites on the same day.
                if streak and streak[0]["new_state"] != feedback["new_state"]:
                    stopped.add(dimension)
                    continue
                streak.append(feedback)
            items = []
            for dimension, evidence in sorted(streaks.items()):
                days = {
                    datetime.fromisoformat(row["occurred_at"])
                    .astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
                    for row in evidence
                }
                required = max(self.settings.emerging_min_sessions, self.settings.stable_min_days)
                stable = len(days) >= required and not truncated
                status = "STABLE" if stable else (
                    "EMERGING" if len(days) >= self.settings.emerging_min_sessions else "CANDIDATE"
                )
                item = {
                    "namespace": "owner.expression_preference",
                    "dimension": dimension,
                    "value": evidence[0]["new_state"],
                    "status": status,
                    "independent_days": len(days),
                    "local_dates": sorted(days),
                    # Evidence coverage, not a calibrated probability of a personality trait.
                    "confidence": round(min(0.9, 0.9 * len(days) / required), 3),
                    "strength": min(0.5, 0.1 * len(days)) if stable else 0.0,
                    "evidence": list(reversed(evidence)),
                }
                item["version"] = version({"policy": POLICY_VERSION,
                                           "timezone": self.settings.timezone, **item})
                items.append(item)
            result = {
                "mode": "SHADOW",
                "policy_version": POLICY_VERSION,
                "privacy_epoch": epoch(s),
                "timezone": self.settings.timezone,
                "truncated": truncated,
                "items": items,
            }
            result["version"] = version(result)
            return result
