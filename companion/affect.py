"""Short-lived interaction continuity from explicit owner feedback, without model polling."""

import json
import re
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from companion import owner_channel
from companion.contracts import CoreError
from companion.store import after, encode, uid

RESET_TARGET = "affect_reset"
RESET_KEY = "owner_feedback_reset_at"


def feedback_reset_at(store):
    row = store.one('SELECT value FROM runtime_meta WHERE key = %s', (RESET_KEY,))
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

    if not store.in_transaction:
        raise RuntimeError("AFFECT_RESET_REQUIRES_TRANSACTION")
    validate_reset_record(record)
    stamp = datetime.fromisoformat(record["at"]).astimezone(UTC).isoformat(timespec="microseconds")
    previous = feedback_reset_at(store)
    cutoff = max(stamp, previous or stamp)
    store.execute('INSERT INTO runtime_meta (key, value) VALUES (%s, %s) ON CONFLICT(key) DO '
        'UPDATE SET value = excluded.value, record_sequence = DEFAULT', (RESET_KEY, cutoff))
    removed = store.execute(
        "DELETE FROM affect_evidence WHERE identity_id = 'companion' AND created_at <= %s",
        (cutoff,),
    )
    if cutoff != previous or removed:
        store.execute("DELETE FROM affect_states WHERE identity_id = 'companion'")
        advance_epoch(store)
    return {"state": "RESET", "reset_at": cutoff}

NEUTRAL = {
    "interaction_tone": "neutral",
    "engagement": "neutral",
    "cognitive_load": "normal",
    "novelty_interest": "unknown",
}
TOPIC_PAUSE = re.compile(
    r"(?:先别聊这个(?:了)?|先不聊这个(?:话题)?了?|暂停这个话题|换个话题吧"
    r"|这个话题(?:先)?(?:别|不要|不)(?:再)?聊了?)")
TODAY_PAUSE = re.compile(
    r"(?:今天|今晚)(?:先)?(?:别|不要|不用)(?:再)?主动(?:找|联系|打扰|发消息给)我(?:了|啦)?")
TOPIC_RESUME = re.compile(r"(?:可以)?继续(?:聊)?这个话题(?:了)?")
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
        rf"(?:{TOPIC_PAUSE.pattern}|(?:我)?先不聊了|目前不想聊|{TODAY_PAUSE.pattern}|"
        r"(?:我)?(?:去上课了?|要上课了|现在(?:很)?忙|在忙|(?:正)?在开会|要开会了|去开会了?)"
        r"[，,、 ]*(?:(?:晚点|稍后)(?:再)?聊)?)",
        "engagement", "pause", "OWNER_REQUESTED_PAUSE",
    ),
    (
        rf"(?:继续聊|{TOPIC_RESUME.pattern}|可以继续|欢迎继续|我回来了[，, ]*(?:可以聊了|继续聊))",
        "engagement", "continue", "OWNER_REQUESTED_CONTINUE",
    ),
    (r"(?:想试试新的话题|换个新话题吧)", "novelty_interest", "open", "OWNER_REQUESTED_NOVELTY"),
)
# Clauses are split only at sentence punctuation. Colons and quotes stay inside a
# clause, so quoted or reported wording never fully matches a rule.
CLAUSE_BREAK = re.compile(r"[，,。！!？?；;~～]+")
NOT_OWN_REQUEST = re.compile(r"[“”\"「」『』]|开玩笑|逗你|不是说|没说|假如|如果")
REPORTING = re.compile(r"(?:说|讲|问|道|写着|提到)$")


def _feedback_clause(text):
    """One unambiguous explicit request among the owner's own clauses, or None."""
    if NOT_OWN_REQUEST.search(text):
        return None
    clauses = [part for part in CLAUSE_BREAK.split(text) if part]
    matches = []
    for index, clause in enumerate(clauses):
        rule = next((rule for rule in RULES if re.fullmatch(rule[0], clause)), None)
        if rule is None:
            continue
        if index and REPORTING.search(clauses[index - 1]):
            return None
        matches.append((clause, rule))
    if not matches:
        return None
    # Contact control outranks wording style; contradictory controls are ignored.
    engagement = [item for item in matches if item[1][1] == "engagement"]
    chosen = engagement or matches
    if len({rule[1:3] for _, rule in chosen}) != 1:
        return None
    return chosen[-1]


def owner_feedback(store, settings, event_id, now):
    """Revalidate original, retained owner wording; no summaries or inferred mood."""
    event = store.one(
        'SELECT e.body_json, e.ingested_at, e.occurred_at FROM events AS e JOIN conversations AS c '
        "USING (conversation_id) WHERE e.event_id = %s AND e.redacted = 0 AND e.actor_id = 'owner' "
        "AND e.source IN ('qq.onebot','native.chat') AND e.privacy_scope = 'private' "
        "AND e.event_type = "
        "'message.received' AND c.scope = 'private' AND c.owner_actor_id = 'owner' AND "
        'LENGTH(e.body_json) <= 16000',
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
    matched = _feedback_clause(text.strip())
    if not matched:
        return None
    clause, (_, dimension, value, cause) = matched
    return {
        **({"until_midnight": True} if TODAY_PAUSE.fullmatch(clause) else {}),
        "topic": bool(TOPIC_PAUSE.fullmatch(clause) or TOPIC_RESUME.fullmatch(clause)),
        "event_id": event_id,
        "dimension": dimension,
        "new_state": value,
        "reason_code": cause,
        "created_at": event["ingested_at"],
        "occurred_at": event["occurred_at"],
    }


def erase_affect(store, event_ids):
    if not store.one('SELECT 1 FROM pg_catalog.pg_class WHERE '
        "relnamespace=current_schema()::regnamespace AND relname='affect_evidence' AND relkind IN "
        "('r','p')"):
        return
    changed = 0
    for event_id in event_ids:
        changed += store.execute(
            'DELETE FROM affect_evidence WHERE event_id = %s', (event_id,)
        )
    if changed:
        store.execute("DELETE FROM affect_states WHERE identity_id = 'companion'")


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
        with nullcontext() if self.store.in_transaction else self.store.transaction(
            control_write=True
        ):
            return ledger.reset_affect(self.store, request_id, self.clock())

    def revalidate(self, snapshot):
        if encode(snapshot) != encode(self.snapshot()):
            raise CoreError("PRIVACY_CHANGED", 409)

    def snapshot(self):
        result = {"state": dict(NEUTRAL), "confidence": 0.0, "evidence": [], "expires_at": None}
        if not self.settings.affect_enabled:
            return result
        rows = self.store.rows(
            'SELECT _0.identity_id AS identity_id, _0.event_id AS event_id, _0.dimension AS '
            'dimension, _0.prior_state AS prior_state, _0.new_state AS new_state, _0.reason_code '
            'AS reason_code, _0.confidence AS confidence, _0.created_at AS created_at, '
            '_0.expires_at AS expires_at, _0.evidence_row AS evidence_row, _0.rank AS rank FROM '
            '(SELECT a.identity_id AS identity_id, a.event_id AS event_id, a.dimension AS '
            'dimension, a.prior_state AS prior_state, a.new_state AS new_state, a.reason_code AS '
            'reason_code, a.confidence AS confidence, a.created_at AS created_at, a.expires_at AS '
            'expires_at, a.record_sequence AS evidence_row, ROW_NUMBER() OVER (PARTITION BY '
            'a.dimension ORDER BY a.record_sequence DESC NULLS LAST) AS rank FROM affect_evidence '
            "AS a JOIN events AS e ON a.event_id = e.event_id WHERE a.identity_id = 'companion' "
            'AND e.redacted = 0 AND a.expires_at > %s AND a.created_at <= %s) AS _0 WHERE _0.rank '
            '<= %s ORDER BY _0.evidence_row DESC NULLS LAST',
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
                "dimension", "new_state", "reason_code", "created_at", "topic",
            )},
            "expires_at": expires,
        }

    def _topic_targets(self, row):
        previous = self.store.one(
            'SELECT event_id, ingested_at FROM events WHERE conversation_id = %s AND '
            "record_sequence < %s AND source IN ('qq.onebot','native.chat') AND "
            "actor_id = 'owner' AND NOT "
            'EXISTS(SELECT 1 FROM affect_evidence AS a WHERE a.event_id = events.event_id AND '
            "a.dimension = 'engagement') ORDER BY record_sequence DESC NULLS LAST LIMIT 1",
            (row["conversation_id"], row["event_row"]),
        )
        # A delivered share can be the subject even hours after the last owner turn.
        sent = self.store.one(
            'SELECT origin_event_id FROM outbox WHERE conversation_id = %s AND action_type IN '
            "" + owner_channel.SQL_PRIVATE_ACTIONS + " AND state = 'SENT' AND NOT EXISTS(SELECT "
            '1 FROM affect_evidence AS a WHERE a.event_id = origin_event_id AND a.dimension = '
            "'engagement') AND sent_at > %s AND sent_at <= %s ORDER BY sent_at DESC NULLS LAST, "
            'record_sequence DESC NULLS LAST LIMIT 1',
            (row["conversation_id"], previous["ingested_at"] if previous else "",
             row["created_at"]),
        )
        targets = {row["event_id"]}
        if sent:
            targets.add(sent["origin_event_id"])
            targets.update(item["source_event_id"] for item in self.store.rows(
                'SELECT source_event_id FROM event_dependencies WHERE event_id = %s',
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
            'SELECT a.identity_id AS identity_id, a.event_id AS event_id, a.dimension AS '
            'dimension, a.prior_state AS prior_state, a.new_state AS new_state, a.reason_code AS '
            'reason_code, a.confidence AS confidence, a.created_at AS created_at, a.expires_at AS '
            'expires_at, e.conversation_id AS conversation_id, e.record_sequence AS event_row FROM '
            'affect_evidence AS a JOIN events AS e ON a.event_id = e.event_id WHERE a.identity_id '
            "= 'companion' AND a.dimension = 'engagement' AND e.redacted = 0 AND a.expires_at > %s "
            'ORDER BY a.record_sequence DESC NULLS LAST LIMIT %s',
            (self.clock(), self.settings.affect_max_evidence),
        ):
            proposal = self.preview(row["event_id"])
            if not proposal or any(proposal[key] != row[key] for key in
                                   ("dimension", "new_state", "reason_code", "expires_at")):
                continue
            if proposal["topic"]:
                if ids and topic is None and ids & self._topic_targets(row):
                    topic = row["new_state"] == "pause"
            elif contact is None:
                contact = row["new_state"] == "pause"
        return bool(contact or topic)

    def observe(self, event_id):
        if not self.settings.affect_enabled:
            return False
        s = self.store
        with nullcontext() if s.in_transaction else s.transaction():
            proposal = self.preview(event_id)
            if proposal is None:
                return False
            dimension, value, cause, expires = (
                proposal[key] for key in ("dimension", "new_state", "reason_code", "expires_at")
            )
            prior = self.snapshot()["state"][dimension]
            changed = s.execute(
                "INSERT INTO affect_evidence VALUES ('companion', %s, %s, %s, %s, %s, %s, %s, %s) "
                'ON CONFLICT DO NOTHING',
                (event_id, dimension, prior, value, cause, 1.0, proposal["created_at"], expires),
            )
            if not changed:
                return False
            # Each dimension keeps its own bounded window, so frequent wording
            # feedback cannot evict a still-current contact or topic pause.
            s.execute(
                (
                    'DELETE FROM affect_evidence WHERE identity_id = '
                    "'companion' AND NOT record_sequence IN (SELECT "
                    'retained.record_sequence FROM (SELECT record_sequence, '
                    'ROW_NUMBER() OVER (PARTITION BY dimension ORDER BY '
                    'record_sequence DESC NULLS LAST) AS rank FROM '
                    "affect_evidence WHERE identity_id = 'companion') AS retained WHERE "
                    'retained.rank <= %s)'
                ),
                (self.settings.affect_max_evidence,),
            )
            current = self.snapshot()
            s.execute(
                (
                    (
                        "INSERT INTO affect_states VALUES ('companion',%s,%s,%s,"
                        '%s,1) ON CONFLICT(identity_id) DO UPDATE SET '
                        'state_json=excluded.state_json,'
                        'confidence=excluded.confidence,'
                        'updated_at=excluded.updated_at,'
                        'expires_at=excluded.expires_at,'
                        'version=affect_states.version+1'
                    )
                ),
                (
                    encode(current["state"]),
                    current["confidence"],
                    self.clock(),
                    current["expires_at"],
                ),
            )
            s.audit("affect.observed", event_id, event_id, self.clock(), reason=cause)
            return True
