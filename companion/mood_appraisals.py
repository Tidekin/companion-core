"""Owner interaction signals judged by the existing memory_extract call, kept for mood.

The model only picks a closed reason and quotes the owner's words; code fixes the PAD
delta, checks the quote against the retained original message and records nothing while
mood is off. Rows are derived data: native erasure removes them with their source event,
and the mood projection re-checks lineage, quote and model settlement on every read.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from companion.paid_budget import sha
from companion.store import after

# P/A/D deltas. Her reaction to the owner's words, never a copy of the owner's feeling:
# distress raises attention, not sadness; hurt makes her want to make up, not sulk.
REASONS = {
    "OWNER_AFFECTION": (0.05, 0.02, 0.01),
    "OWNER_HURT_BY_REPLY": (-0.04, 0.03, -0.03),
    "OWNER_DISTRESS": (0.0, 0.03, 0.0),
    "OWNER_EXPECTATION": (0.0, 0.025, 0.0),
    "OWNER_PLAYFUL": (0.03, 0.02, 0.0),
}
DAILY_CAP = 2
HURT_REPLY_WINDOW = 3600


def quote_digest(quote):
    return sha(quote.strip())


def owner_text(store, event_id):
    """The retained private owner wording, or None for anything that is not a plain message."""
    event = store.one(
        "SELECT body_json FROM events WHERE event_id=? AND redacted=0 AND actor_id='owner' "
        "AND source='qq.onebot' AND privacy_scope='private' AND event_type='message.received' "
        "AND length(body_json)<=16000",
        (event_id,),
    )
    if not event:
        return None
    body = json.loads(event["body_json"])
    if (
        not isinstance(body, dict)
        or body.get("control_command")
        or body.get("diary_command")
        or body.get("quote")
        or body.get("rich")
        or body.get("shared_content")
    ):
        return None
    text = body.get("text", "")
    return text if isinstance(text, str) and text.strip() else None


def target_digest(reply):
    """Fields that stay fixed once a message is SENT; lease and retry columns are excluded."""
    return sha({key: reply[key] for key in (
        "message_id", "origin_event_id", "conversation_id", "action_type", "content_sha256",
        "sent_at",
    )})


def reply_target(store, event_id):
    """The one delivered message, sent within the hour before this owner message, that the
    hurt is about. A possibly delivered newer message makes the target ambiguous: no guess."""
    event = store.one(
        "SELECT conversation_id,occurred_at FROM events WHERE event_id=?", (event_id,)
    )
    if not event:
        return None
    reply = store.one(
        "SELECT * FROM outbox WHERE conversation_id=? AND state='SENT' "
        "AND action_type IN ('qq.private.reply','qq.private.proactive') "
        "AND sent_at<=? AND sent_at>? ORDER BY sent_at DESC,rowid DESC LIMIT 1",
        (
            event["conversation_id"],
            event["occurred_at"],
            after(event["occurred_at"], -HURT_REPLY_WINDOW),
        ),
    )
    if not reply or store.one(
        "SELECT 1 FROM outbox WHERE conversation_id=? "
        "AND action_type IN ('qq.private.reply','qq.private.proactive') "
        "AND state IN ('SENDING','DELIVERY_UNCERTAIN') AND created_at>=? AND created_at<=? "
        "LIMIT 1",
        (event["conversation_id"], reply["created_at"], event["occurred_at"]),
    ):
        return None
    return reply


def validate(store, signals, event_ids, rejected=None):
    """Keep at most one signal per owner message whose quote is really in that message.

    `rejected`, when given, counts each dropped signal by a content-free code.
    """
    kept, seen = [], set()
    rejected = {} if rejected is None else rejected

    def drop(code):
        rejected[code] = rejected.get(code, 0) + 1

    for signal in signals:
        if signal.event_id not in event_ids:
            drop("OUTSIDE_BATCH")
            continue
        if signal.event_id in seen:
            drop("DUPLICATE_EVENT")
            continue
        text, quote = owner_text(store, signal.event_id), signal.quote.strip()
        start = text.find(quote) if text is not None and len(quote) >= 2 else -1
        if start < 0:
            drop("NOT_OWNER_TEXT" if text is None else "QUOTE_NOT_FOUND")
            continue
        target = None
        if signal.reason == "OWNER_HURT_BY_REPLY":
            target = reply_target(store, signal.event_id)
            if not target:
                drop("NO_REPLY_TARGET")
                continue
        seen.add(signal.event_id)
        kept.append(
            {
                "event_id": signal.event_id,
                "reason": signal.reason,
                "quote_start": start,
                "quote_length": len(quote),
                "quote_sha256": quote_digest(quote),
                "target_message_id": target and target["message_id"],
                "target_sha256": target and target_digest(target),
            }
        )
    return kept


def record(store, settings, signals, request_id, now):
    """Commit the validated extraction result independently of the fact review."""
    if not store.db.in_transaction:
        raise RuntimeError("MOOD_APPRAISAL_REQUIRES_TRANSACTION")
    inserted = 0
    for item in signals if settings.mood_enabled else ():
        inserted += store.execute(
            "INSERT OR IGNORE INTO mood_appraisals(event_id,reason,quote_start,quote_length,"
            "quote_sha256,request_id,created_at,target_message_id,target_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                item["event_id"],
                item["reason"],
                item["quote_start"],
                item["quote_length"],
                item["quote_sha256"],
                request_id,
                now,
                item.get("target_message_id"),
                item.get("target_sha256"),
            ),
        ).rowcount
    if signals:
        store.audit("mood.appraisals_committed", request_id, "memory", now, inserted=inserted,
                    already_recorded=len(signals) - inserted if settings.mood_enabled else 0,
                    disabled=0 if settings.mood_enabled else len(signals))


def erase(store, event_ids):
    """Native erasure/replay cleanup: appraisals die with their source message."""
    if not store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mood_appraisals'"):
        return
    store.execute(
        "DELETE FROM mood_appraisals WHERE event_id IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(event_ids)),),
    )


def still_valid(store, row):
    """Re-check on every projection read; a later edit, redaction or unsettled call drops it."""
    text = owner_text(store, row["event_id"])
    usage = store.one(
        "SELECT state,scenario FROM model_usage WHERE request_id=?", (row["request_id"],)
    )
    if (
        text is None
        or not usage
        or usage["state"] != "SETTLED"
        or usage["scenario"] != "memory_extract"
        or row["reason"] not in REASONS
    ):
        return False
    if row["reason"] == "OWNER_HURT_BY_REPLY":
        # The same reply must still be the unambiguous target and unchanged since judging.
        target = reply_target(store, row["event_id"])
        if (
            not target
            or target["message_id"] != row["target_message_id"]
            or target_digest(target) != row["target_sha256"]
        ):
            return False
    quote = text[row["quote_start"] : row["quote_start"] + row["quote_length"]]
    return len(quote) == row["quote_length"] and quote_digest(quote) == row["quote_sha256"]


def capped(signals, timezone):
    """At most DAILY_CAP valid signals per reason per local day of the owner's message,
    oldest first, deterministically. Callers drop invalid ones first so they use no quota."""
    zone, counts, kept = ZoneInfo(timezone), {}, []
    for item in sorted(signals, key=lambda value: (value["at"], value["event_id"])):
        day = datetime.fromisoformat(item["at"]).astimezone(zone).date().isoformat()
        key = (day, item["reason"])
        if counts.get(key, 0) < DAILY_CAP:
            counts[key] = counts.get(key, 0) + 1
            kept.append(item)
    return kept
