"""Ordering-only salience for recall: owner mentions keep a memory vivid, mood colours order.

Nothing here makes a memory eligible, changes its text or counts as evidence. Bonuses are
small next to the reciprocal-rank fusion gaps, and an exact match still always comes first.
A mention is the owner's own message literally touching the memory; her recalls and the
bonus itself never count, so salience cannot feed itself.
"""

import json
import math
from datetime import datetime

from companion import mood_appraisals

MENTION_HALF_LIFE = 30 * 86400
MENTION_LOOKBACK = 180 * 86400
REPEAT_WINDOW = 86400
VIVID_CAP = 4.0
# Reciprocal-rank scores near the top differ by ~0.00026 per rank; these move a few ranks.
VIVID_WEIGHT = 0.0008
MOOD_WEIGHT = 0.0005
MOOD_FULL = 0.1
MIN_TERMS = 2


def _age(now, at):
    return (datetime.fromisoformat(now) - datetime.fromisoformat(at)).total_seconds()


def vividness(times, now):
    """Recent owner mentions, each fading over 30 days; repeats within a day are dulled."""
    strength, recent = 0.0, []
    for at in sorted(times):
        recent = [t for t in recent if 0 <= _age(at, t) < REPEAT_WINDOW]
        age = _age(now, at)
        if 0 <= age <= MENTION_LOOKBACK:
            strength += 0.5 ** len(recent) * 2 ** (-age / MENTION_HALF_LIFE)
        recent.append(at)
    return min(strength, VIVID_CAP)


def congruence(mood, tag):
    """Cosine of current expression and a memory's appraisal colour, positive part only."""
    if not mood or not tag:
        return 0.0
    current = [mood.get(key, 0.0) for key in ("pleasure", "arousal", "dominance")]
    size, other = math.hypot(*current), math.hypot(*tag)
    if not size or not other:
        return 0.0
    cosine = sum(a * b for a, b in zip(current, tag, strict=True)) / (size * other)
    return max(0.0, cosine) * min(1.0, size / MOOD_FULL)


def boosts(store, memory_ids, now, mood=None):
    """memory_id -> {"bonus", "vividness", "mood"} for the already eligible candidates."""
    ids = sorted(set(memory_ids))
    if not ids:
        return {}
    times = {}
    for row in store.rows(
        "SELECT memory_id, mentioned_at FROM memory_mentions WHERE memory_id = ANY(%s::text[]) "
        "AND mentioned_at <= %s ORDER BY memory_id, mentioned_at",
        (ids, now),
    ):
        times.setdefault(row["memory_id"], []).append(row["mentioned_at"])
    tags = {}
    if mood:
        for row in store.rows(
            "SELECT v.memory_id, a.reason FROM memory_evidence v JOIN mood_appraisals a "
            "ON a.event_id = v.event_id WHERE v.memory_id = ANY(%s::text[]) AND v.polarity = 1 "
            "ORDER BY v.memory_id, a.event_id",
            (ids,),
        ):
            delta = mood_appraisals.REASONS.get(row["reason"])
            if delta:
                tag = tags.setdefault(row["memory_id"], [0.0, 0.0, 0.0])
                for i, value in enumerate(delta):
                    tag[i] += value
    result = {}
    for memory_id in ids:
        vivid = vividness(times.get(memory_id, []), now)
        fit = congruence(mood, tags.get(memory_id))
        bonus = VIVID_WEIGHT * math.log2(1 + vivid) / math.log2(1 + VIVID_CAP) + MOOD_WEIGHT * fit
        if bonus:
            result[memory_id] = {"bonus": bonus, "vividness": round(vivid, 4),
                                 "mood": round(fit, 4)}
    return result


def mentioned(text, content):
    """The owner's message literally shares at least two terms with the memory."""
    from companion.memory_candidates import terms

    if not text or not content:
        return False
    return sum(1 for term in terms(text) if term in content) >= MIN_TERMS


def record(store, job, rows, now):
    """Log owner mentions for memories put into this owner chat reply's context.

    A concurrently erased message or forgotten memory leaves nothing behind.
    """
    body = json.loads(job.get("body_json") or "{}")
    text = body.get("text", "") if isinstance(body, dict) else ""
    for row in rows:
        if mentioned(text, row.get("content", "")):
            store.execute(
                "INSERT INTO memory_mentions(memory_id, event_id, mentioned_at) SELECT %s, %s, %s "
                "WHERE EXISTS(SELECT 1 FROM events WHERE event_id = %s AND redacted = 0 AND "
                "actor_id = 'owner') AND EXISTS(SELECT 1 FROM memories WHERE memory_id = %s AND "
                "status IN ('CONFIRMED','HYPOTHESIS')) ON CONFLICT DO NOTHING",
                (row["memory_id"], job["event_id"], now, job["event_id"], row["memory_id"]),
            )


def remember(store, job, rows):
    """Owner chat replies only; the mention time is when the owner's message arrived."""
    if not rows or job.get("kind") != "chat" or not job.get("event_id"):
        return
    if store.in_transaction:
        record(store, job, rows, job["created_at"])
        return
    with store.transaction():
        record(store, job, rows, job["created_at"])


def salience_for(core, job, text):
    """Current mood expression for ordering; serious or sensitive turns get none."""
    from companion.contracts import CoreError

    mood = None
    if core.settings.mood_enabled and job.get("kind") == "chat":
        try:
            reference = core.mood.reference(at=job["created_at"], text=text)
        except CoreError:
            reference = None
        mood = reference["state"] if reference else None
    return {"mood": mood}


def erase(store, event_ids):
    """A mention dies with the owner message that made it."""
    store.execute(
        'DELETE FROM memory_mentions WHERE event_id IN (SELECT value COLLATE "C" FROM '
        "JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS parsed(value))",
        (json.dumps(sorted(event_ids)),),
    )


def erase_memories(store, memory_ids):
    store.execute("DELETE FROM memory_mentions WHERE memory_id = ANY(%s::text[])",
                  (sorted(memory_ids),))
