"""Short-lived interaction continuity from explicit owner feedback, without model polling."""

import json
import re
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from companion.contracts import CoreError
from companion.store import after, encode, uid

RESET_TARGET = "affect_reset"
RESET_KEY = "owner_feedback_reset_at"


def feedback_reset_at(store):
    row = store.one("SELECT value FROM runtime_meta WHERE key=?", (RESET_KEY,))
    if row is None:
        return None
    try:
        stamp = datetime.fromisoformat(row["value"])
        if stamp.utcoffset() is None:
            raise ValueError("reset timestamp needs timezone")
        return stamp.astimezone(UTC).isoformat(timespec="microseconds")
    except (TypeError, ValueError) as error:
        raise CoreError("AFFECT_RESET_INVALID", 503) from error


def reset_target(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
        raise CoreError("AFFECT_RESET_ID_REQUIRED", 400)
    return RESET_TARGET + ":" + request_id


def reset_record(sequence, request_id, now):
    """Native Ledger record payload; the Ledger owns append/fsync/replay."""
    stamp = datetime.fromisoformat(now)
    if stamp.utcoffset() is None:
        raise CoreError("AFFECT_RESET_INVALID", 400)
    return {"seq": sequence, "erasure_id": uid("erase"),
            "memory_id": reset_target(request_id), "memory_ids": [], "event_ids": [],
            "at": stamp.astimezone(UTC).isoformat(timespec="microseconds"),
            "target_type": RESET_TARGET}


def validate_reset_record(record):
    if (record.get("target_type") != RESET_TARGET or record.get("event_ids") != []
        or record.get("memory_ids") != []
        or not isinstance(record.get("memory_id"), str)
        or not record["memory_id"].startswith(RESET_TARGET + ":")):
        raise ValueError("invalid feedback reset scope")
    try:
        reset_target(record["memory_id"][len(RESET_TARGET) + 1:])
        if datetime.fromisoformat(record["at"]).utcoffset() is None:
            raise ValueError("reset timestamp needs timezone")
    except (CoreError, KeyError, TypeError) as error:
        raise ValueError("invalid feedback reset record") from error


def apply_feedback_reset(store, record):
    """Idempotent, monotonic projection; no event/fact/trait/autonomy deletion."""
    from companion.privacy import advance_epoch

    if not store.db.in_transaction:
        raise RuntimeError("AFFECT_RESET_REQUIRES_TRANSACTION")
    validate_reset_record(record)
    stamp = datetime.fromisoformat(record["at"]).astimezone(UTC).isoformat(timespec="microseconds")
    previous = feedback_reset_at(store)
    cutoff = max(stamp, previous or stamp)
    store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)", (RESET_KEY, cutoff))
    removed = store.execute(
        "DELETE FROM affect_evidence WHERE identity_id='companion' AND created_at<=?",
        (cutoff,),
    ).rowcount
    if cutoff != previous or removed:
        store.execute("DELETE FROM affect_states WHERE identity_id='companion'")
        advance_epoch(store)
    return {"state": "RESET", "reset_at": cutoff}

NEUTRAL = {
    "interaction_tone": "neutral",
    "engagement": "neutral",
    "cognitive_load": "normal",
    "novelty_interest": "unknown",
}
TOPIC_PAUSE = re.compile(r"先别聊这个|暂停这个话题|换个话题吧")
RULES = (
    (
        r"(?:请)?(?:回答)?(?:简短一点|简洁一点|说短一点|更简短)",
        "interaction_tone",
        "concise",
        "OWNER_REQUESTED_CONCISE",
    ),
    (
        r"(?:请)?(?:详细解释|展开说说|说详细一点|更详细)",
        "interaction_tone",
        "detailed",
        "OWNER_REQUESTED_DETAIL",
    ),
    (
        r"(?:请)?(?:分步骤说|一步一步来|先讲一步)",
        "cognitive_load",
        "stepwise",
        "OWNER_REQUESTED_STEPS",
    ),
    (
        rf"(?:{TOPIC_PAUSE.pattern}|先不聊了|目前不想聊|今天别主动找我|"
        r"(?:我)?(?:去上课了?|现在忙|在忙)[，,、 ]*(?:晚点聊|稍后聊)?)",
        "engagement", "pause", "OWNER_REQUESTED_PAUSE",
    ),
    (
        r"(?:继续聊|继续这个话题|可以继续|欢迎继续|我回来了，可以聊了)",
        "engagement", "continue", "OWNER_REQUESTED_CONTINUE",
    ),
    (r"(?:想试试新的话题|换个新话题吧)", "novelty_interest", "open", "OWNER_REQUESTED_NOVELTY"),
)


def owner_feedback(store, settings, event_id, now):
    """Revalidate original, retained owner wording; no summaries or inferred mood."""
    event = store.one(
        "SELECT e.body_json,e.ingested_at,e.occurred_at FROM events e "
        "JOIN conversations c USING(conversation_id) WHERE e.event_id=? AND e.redacted=0 "
        "AND e.actor_id='owner' AND e.source='qq.onebot' AND e.privacy_scope='private' "
        "AND e.event_type='message.received' AND c.scope='private' "
        "AND c.owner_actor_id='owner' AND length(e.body_json)<=16000",
        (event_id,),
    )
    if not event or not (
        after(now, -settings.raw_event_retention_days * 86400) < event["ingested_at"] <= now
        and event["occurred_at"] <= now
    ):
        return None
    cutoff = feedback_reset_at(store)
    if cutoff is not None and datetime.fromisoformat(event["ingested_at"]) <= (
        datetime.fromisoformat(cutoff)
    ):
        return None
    try:
        body = json.loads(event["body_json"])
    except ValueError:
        return None
    if (not isinstance(body, dict) or body.get("diary_command") or body.get("control_command")
        or body.get("rich") or body.get("quote")):
        return None
    text = body.get("text", "")
    if not isinstance(text, str):
        return None
    text = text.strip().rstrip("。！!？? ")
    matched = next((rule for rule in RULES if re.fullmatch(rule[0], text)), None)
    if not matched:
        return None
    _, dimension, value, cause = matched
    return {
        **({"until_midnight": True} if text == "今天别主动找我" else {}),
        "event_id": event_id,
        "dimension": dimension,
        "new_state": value,
        "reason_code": cause,
        "created_at": event["ingested_at"],
        "occurred_at": event["occurred_at"],
    }


def erase_affect(store, event_ids):
    if not store.one("SELECT 1 FROM sqlite_master WHERE name='affect_evidence' AND type='table'"):
        return
    changed = 0
    for event_id in event_ids:
        changed += store.execute(
            "DELETE FROM affect_evidence WHERE event_id=?", (event_id,)
        ).rowcount
    if changed:
        store.execute("DELETE FROM affect_states WHERE identity_id='companion'")


class AffectEngine:
    def __init__(self, store, settings, clock):
        self.store, self.settings, self.clock = store, settings, clock

    def reset(self, requester, request_id, ledger):
        if requester != self.settings.owner_qq_id:
            raise CoreError("OWNER_REQUIRED", 403)
        reset_target(request_id)
        if not callable(getattr(ledger, "reset_affect", None)):
            raise CoreError("AFFECT_RESET_NOT_INTEGRATED", 503)
        # Also available while Affect is disabled; reset cannot enable a feature.
        with nullcontext() if self.store.db.in_transaction else self.store.transaction():
            return ledger.reset_affect(self.store, request_id, self.clock())

    def revalidate(self, snapshot):
        if encode(snapshot) != encode(self.snapshot()):
            raise CoreError("PRIVACY_CHANGED", 409)

    def snapshot(self):
        result = {"state": dict(NEUTRAL), "confidence": 0.0, "evidence": [], "expires_at": None}
        if not self.settings.affect_enabled:
            return result
        rows = self.store.rows(
            "SELECT a.* FROM affect_evidence a JOIN events e USING(event_id) "
            "WHERE identity_id='companion' AND e.redacted=0 AND a.expires_at>? "
            "AND a.created_at<=? ORDER BY a.rowid DESC LIMIT ?",
            (self.clock(), self.clock(), self.settings.affect_max_evidence),
        )
        seen = set()
        valid = []
        for row in rows:
            proposed = self.preview(row["event_id"])
            if proposed is None or any(
                row[key] != proposed[key]
                for key in ("dimension", "new_state", "reason_code", "expires_at")
            ):
                continue
            if row["dimension"] in seen:
                continue
            valid.append(row)
            seen.add(row["dimension"])
            result["state"][row["dimension"]] = row["new_state"]
            result["evidence"].append(
                {k: row[k] for k in ("event_id", "dimension", "reason_code", "expires_at")}
            )
        if valid:
            result["confidence"] = 1.0
            result["expires_at"] = min(row["expires_at"] for row in valid)
        return result

    def preview(self, event_id):
        """Read-only shadow: direct private owner feedback, never inferred emotion."""
        feedback = owner_feedback(self.store, self.settings, event_id, self.clock())
        if feedback is None:
            return None
        expires = min(
            after(feedback["created_at"], self.settings.affect_decay_minutes * 60),
            after(feedback["created_at"], self.settings.raw_event_retention_days * 86400),
        )
        if feedback.get("until_midnight"):
            local = datetime.fromisoformat(feedback["created_at"]).astimezone(
                ZoneInfo(self.settings.timezone))
            tomorrow = (local + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            expires = min(tomorrow.astimezone(UTC).isoformat(timespec="microseconds"),
                          after(feedback["created_at"],
                                self.settings.raw_event_retention_days * 86400))
        if not feedback["created_at"] <= self.clock() < expires:
            return None
        return {
            "namespace": "owner.feedback",
            "mode": "SHADOW",
            "event_id": event_id,
            **{key: feedback[key] for key in (
                "dimension", "new_state", "reason_code", "created_at",
            )},
            "expires_at": expires,
        }

    def _topic_targets(self, row):
        previous = self.store.one(
            "SELECT event_id,ingested_at FROM events WHERE conversation_id=? AND rowid<? "
            "AND source='qq.onebot' AND actor_id='owner' AND NOT EXISTS (SELECT 1 FROM "
            "affect_evidence a WHERE a.event_id=events.event_id AND a.dimension='engagement') "
            "ORDER BY rowid DESC LIMIT 1",
            (row["conversation_id"], row["event_row"]),
        )
        # A delivered share can be the subject even hours after the last owner turn.
        sent = self.store.one(
            "SELECT origin_event_id FROM outbox WHERE conversation_id=? "
            "AND action_type IN ('qq.private.reply','qq.private.proactive') AND state='SENT' "
            "AND NOT EXISTS (SELECT 1 FROM affect_evidence a WHERE a.event_id=origin_event_id "
            "AND a.dimension='engagement') "
            "AND sent_at>? AND sent_at<=? ORDER BY sent_at DESC,rowid DESC LIMIT 1",
            (row["conversation_id"], previous["ingested_at"] if previous else "",
             row["created_at"]),
        )
        targets = {row["event_id"]}
        if sent:
            targets.add(sent["origin_event_id"])
            targets.update(item["source_event_id"] for item in self.store.rows(
                "SELECT source_event_id FROM event_dependencies WHERE event_id=?",
                (sent["origin_event_id"],)))
        elif previous:
            targets.add(previous["event_id"])
        return targets

    def contact_paused(self, source_ids=()):
        """Apply the latest explicit feedback for contact and for this exchange separately."""
        if not self.settings.affect_enabled:
            return False
        ids = set(source_ids)
        contact = topic = None
        for row in self.store.rows(
            "SELECT a.*,e.conversation_id,e.rowid AS event_row,json_extract(e.body_json,'$.text') "
            "AS wording FROM affect_evidence a JOIN events e USING(event_id) "
            "WHERE a.identity_id='companion' AND a.dimension='engagement' AND e.redacted=0 "
            "AND a.expires_at>? ORDER BY a.rowid DESC LIMIT ?",
            (self.clock(), self.settings.affect_max_evidence),
        ):
            proposal = self.preview(row["event_id"])
            if not proposal or any(proposal[key] != row[key] for key in
                                   ("dimension", "new_state", "reason_code", "expires_at")):
                continue
            wording = row["wording"].strip().rstrip("。！!？? ")
            if TOPIC_PAUSE.fullmatch(wording) or wording == "继续这个话题":
                if ids and topic is None and ids & self._topic_targets(row):
                    topic = row["new_state"] == "pause"
            elif contact is None:
                contact = row["new_state"] == "pause"
        return bool(contact or topic)

    def observe(self, event_id):
        if not self.settings.affect_enabled:
            return False
        s = self.store
        with nullcontext() if s.db.in_transaction else s.transaction():
            proposal = self.preview(event_id)
            if proposal is None:
                return False
            dimension, value, cause, expires = (
                proposal[key] for key in ("dimension", "new_state", "reason_code", "expires_at")
            )
            prior = self.snapshot()["state"][dimension]
            changed = s.execute(
                "INSERT OR IGNORE INTO affect_evidence VALUES ('companion',?,?,?,?,?,?,?,?)",
                (event_id, dimension, prior, value, cause, 1.0, proposal["created_at"], expires),
            ).rowcount
            if not changed:
                return False
            s.execute(
                "DELETE FROM affect_evidence WHERE identity_id='companion' AND rowid NOT IN "
                "(SELECT rowid FROM affect_evidence WHERE identity_id='companion' "
                "ORDER BY rowid DESC LIMIT ?)",
                (self.settings.affect_max_evidence,),
            )
            current = self.snapshot()
            s.execute(
                "INSERT INTO affect_states VALUES ('companion',?,?,?,?,1) "
                "ON CONFLICT(identity_id) DO UPDATE SET state_json=excluded.state_json,"
                "confidence=excluded.confidence,updated_at=excluded.updated_at,"
                "expires_at=excluded.expires_at,version=version+1",
                (
                    encode(current["state"]),
                    current["confidence"],
                    self.clock(),
                    current["expires_at"],
                ),
            )
            s.audit("affect.observed", event_id, event_id, self.clock(), reason=cause)
            return True
