"""Keepsakes: a short excerpt of the owner's own words outlives source retention.

Only an explicit owner grant (`OwnerGrantPolicy.keepsake`) enables this. At expiry a kept
message is trimmed to the one or two sentences that support its memory instead of being
emptied; everything else derived from the full text is cleared as if it had expired. The
Ledger record carries only hashes, and replay recomputes the same excerpt from the restored
original or expires the message when it no longer matches.
"""

import hashlib
import json
import re

from companion.contracts import CoreError
from companion.memory_salience import words
from companion.store import after, encode

SUBJECTS = ("user", "relationship", "project")
# Recently kept or recently mentioned keepsakes are recalled as usual; older ones only when
# the owner's words literally touch them or she looks them up herself.
RECOLLECTION = "只记得大概：很久以前的事，原话只剩一小段；用“我记得你好像说过”的语气，不编细节"
FRESH_DAYS = 365
MENTION_FRESH_DAYS = 90
SALIENCE_MIN = 0.9
MENTION_DAYS = 3
# Derived records the kept memory still stands on; every other projection is cleared.
PRESERVED = frozenset({
    "owner_authority", "owner_input", "memory-time", "memory-references",
    "memory-enrichment", "memory-mentions", "keepsakes",
})
SENTENCE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def grant(store, settings, clock):
    """The current owner keepsake grant, or None (no authority, revoked, or not given)."""
    from companion import owner_authority

    if not owner_authority.required(store, settings):
        return None
    try:
        view = owner_authority.current(store, settings, clock, purpose="memory")
    except CoreError:
        return None
    if view.policy.memory_scope != "owner_private":
        return None
    return view.policy.keepsake


def excerpt(text, content, limit):
    """The one or two original sentences sharing the most words with the memory."""
    wanted = words(content)
    scored = []
    for index, match in enumerate(SENTENCE.finditer(text or "")):
        sentence = match.group().strip()
        overlap = len(words(sentence) & wanted)
        if sentence and overlap:
            scored.append((overlap, -index, index, sentence))
    if not scored:
        return ""
    best = sorted(scored, reverse=True)[:2]
    picked = sorted(best, key=lambda item: item[2])
    joined = "".join(item[3] for item in picked)
    if len(joined) > limit:
        joined = max(best)[3]
    return joined[:limit].strip()


def active(store, event_ids, now):
    """Kept events still protected: unexpired, and their memory is still a live fact."""
    if not event_ids:
        return set()
    return {row["event_id"] for row in store.rows(
        "SELECT k.event_id FROM keepsakes k JOIN memories m USING (memory_id) "
        "WHERE k.event_id = ANY(%s::text[]) AND (k.until IS NULL OR k.until > %s) "
        "AND m.status IN ('CONFIRMED','HYPOTHESIS') AND m.confidence > 0",
        (sorted(event_ids), now),
    )}


def protected_sql():
    """Predicate for Runtime expiry selection; bound with (now,)."""
    return ("NOT EXISTS(SELECT 1 FROM keepsakes k JOIN memories m USING (memory_id) "
            "WHERE k.event_id = events.event_id AND (k.until IS NULL OR k.until > %s) "
            "AND m.status IN ('CONFIRMED','HYPOTHESIS') AND m.confidence > 0)")


def _mention_days(store, memory_id):
    row = store.one(
        "SELECT COUNT(DISTINCT LEFT(mentioned_at, 10)) AS n FROM memory_mentions "
        "WHERE memory_id = %s", (memory_id,))
    return row["n"] if row else 0


def plan(store, settings, policy, event_ids, cutoff, now):
    """Entries for the expiring messages that are the last support of a memory worth keeping."""
    from companion.observation import expired_sources
    from companion.privacy import memory_sensitive

    if policy is None or not event_ids:
        return []
    batch = set(event_ids)
    held = store.one("SELECT COUNT(*) AS n FROM keepsakes")["n"]
    room = max(0, policy.max_items - held)
    if not room:
        return []
    candidates = {}
    for row in store.rows(
        "SELECT v.memory_id, v.event_id, e.ingested_at, e.body_json, m.content, m.subject, "
        "m.status, m.needs_review, m.importance FROM memory_evidence v JOIN events e "
        "USING (event_id) JOIN conversations c USING (conversation_id) JOIN memories m "
        "USING (memory_id) WHERE v.event_id = ANY(%s::text[]) AND v.polarity = 1 "
        "AND e.redacted = 0 AND e.actor_id = 'owner' AND e.privacy_scope = 'private' "
        "AND e.event_type = 'message.received' AND e.source IN ('qq.onebot','native.chat') "
        "AND c.scope = 'private' AND c.owner_actor_id = 'owner' AND m.privacy_scope = 'private' "
        "AND m.confidence > 0 AND m.subject = ANY(%s::text[]) "
        "ORDER BY v.memory_id, e.ingested_at DESC, v.event_id",
        (sorted(batch), list(SUBJECTS)),
    ):
        if row["memory_id"] in candidates:
            continue
        if row["status"] not in {"CONFIRMED", "HYPOTHESIS"} or row["needs_review"]:
            continue
        if store.one("SELECT 1 FROM keepsakes WHERE memory_id = %s", (row["memory_id"],)):
            continue
        others = [item["event_id"] for item in store.rows(
            "SELECT event_id FROM memory_evidence WHERE memory_id = %s AND polarity = 1 "
            "AND NOT event_id = ANY(%s::text[])", (row["memory_id"], sorted(batch)))]
        if any(not expired_sources(store, [other], cutoff) for other in others):
            continue  # Still supported after this batch; nothing to keep.
        body = json.loads(row["body_json"])
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or memory_sensitive(text):
            continue
        days = _mention_days(store, row["memory_id"])
        if row["importance"] >= SALIENCE_MIN:
            reason = "SALIENCE"
        elif days >= MENTION_DAYS:
            reason = "MENTIONS"
        else:
            continue
        cut = excerpt(text, row["content"], policy.max_excerpt_chars)
        if not cut:
            continue
        candidates[row["memory_id"]] = {
            "event_id": row["event_id"], "memory_id": row["memory_id"], "reason": reason,
            "excerpt_sha256": digest(cut), "limit": policy.max_excerpt_chars,
            "until": (None if policy.retention_days is None
                      else after(row["ingested_at"], policy.retention_days * 86400)),
            "rank": (row["importance"], days),
        }
    chosen = sorted(candidates.values(), key=lambda item: (item["rank"], item["memory_id"]),
                    reverse=True)[:room]
    for item in chosen:
        item.pop("rank")
    return sorted(chosen, key=lambda item: item["event_id"])


def apply(store, entries, now):
    """Trim each kept message; return (kept event ids, event ids that must expire instead)."""
    kept, failed = set(), set()
    for entry in entries:
        event = store.one(
            "SELECT event_id, body_json, redacted FROM events WHERE event_id = %s",
            (entry["event_id"],))
        memory = store.one(
            "SELECT content, status FROM memories WHERE memory_id = %s", (entry["memory_id"],))
        body = json.loads(event["body_json"]) if event else None
        if (not event or event["redacted"] or not memory
                or memory["status"] not in {"CONFIRMED", "HYPOTHESIS"}
                or not isinstance(body, dict) or not isinstance(body.get("text"), str)):
            failed.add(entry["event_id"])
            continue
        marker = body.get("keepsake")
        cut = (body["text"] if isinstance(marker, dict)
               and marker.get("excerpt_sha256") == entry["excerpt_sha256"]
               else excerpt(body["text"], memory["content"], entry["limit"]))
        if not cut or digest(cut) != entry["excerpt_sha256"]:
            failed.add(entry["event_id"])
            continue
        receipt = store.one(
            "SELECT value FROM runtime_meta WHERE key = %s", ("owner_input:" + entry["event_id"],))
        original = (json.loads(receipt["value"]).get("input_sha256") if receipt
                    else None) if not isinstance(marker, dict) else marker.get("input_sha256")
        store.execute(
            "UPDATE events SET body_json = %s WHERE event_id = %s",
            (encode({"text": cut, "keepsake": {
                "memory_id": entry["memory_id"], "excerpt_sha256": entry["excerpt_sha256"],
                "input_sha256": original}}), entry["event_id"]),
        )
        # The excerpt supports only the memory it was kept for.
        store.execute(
            "DELETE FROM memory_evidence WHERE event_id = %s AND memory_id <> %s",
            (entry["event_id"], entry["memory_id"]))
        inserted = store.execute(
            "INSERT INTO keepsakes(event_id, memory_id, excerpt_sha256, reason, kept_at, until) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (entry["event_id"], entry["memory_id"], entry["excerpt_sha256"], entry["reason"],
             now, entry["until"]))
        if inserted:
            store.audit("memory.keepsake_kept", entry["memory_id"], entry["event_id"], now,
                        reason=entry["reason"])
        kept.add(entry["event_id"])
    return kept, failed


def release(store, event_ids):
    """A kept message that now expires takes its keepsake record with it."""
    if event_ids:
        store.execute("DELETE FROM keepsakes WHERE event_id = ANY(%s::text[])",
                      (sorted(event_ids),))


def erase_memories(store, memory_ids):
    if memory_ids:
        store.execute("DELETE FROM keepsakes WHERE memory_id = ANY(%s::text[])",
                      (sorted(memory_ids),))


def erase(store, event_ids):
    """Native erasure of the message removes its keepsake (the event is erased anyway)."""
    release(store, set(event_ids))


def kept_input(body):
    """The original input digest a trimmed keepsake body stands in for, if any."""
    marker = body.get("keepsake") if isinstance(body, dict) else None
    if not isinstance(marker, dict) or not isinstance(body.get("text"), str):
        return None
    if digest(body["text"]) != marker.get("excerpt_sha256"):
        return None
    return marker.get("input_sha256")


def states(store, memory_ids, now):
    """memory_id -> {"state": "FRESH"|"DORMANT", "until"} for kept memories."""
    if not memory_ids:
        return {}
    rows = store.rows(
        "SELECT k.memory_id, k.kept_at, k.until, (SELECT MAX(mentioned_at) FROM memory_mentions "
        "AS n WHERE n.memory_id = k.memory_id) AS mentioned FROM keepsakes k "
        "WHERE k.memory_id = ANY(%s::text[]) AND (k.until IS NULL OR k.until > %s)",
        (sorted(set(memory_ids)), now))
    fresh_kept, fresh_mention = after(now, -FRESH_DAYS * 86400), after(
        now, -MENTION_FRESH_DAYS * 86400)
    return {row["memory_id"]: {
        "state": "FRESH" if row["kept_at"] > fresh_kept or (
            row["mentioned"] and row["mentioned"] > fresh_mention) else "DORMANT",
        "until": row["until"]} for row in rows}


def capacity(store, settings, clock):
    """{"held", "max_items"} under a current grant, else None."""
    policy = grant(store, settings, clock)
    if policy is None:
        return None
    return {"held": store.one("SELECT COUNT(*) AS n FROM keepsakes")["n"],
            "max_items": policy.max_items}


def release_by_owner(store, settings, ledger, memory_id, now):
    """The owner lets a keepsake go: its excerpt expires now through the native Ledger."""
    row = store.one("SELECT event_id FROM keepsakes WHERE memory_id = %s", (memory_id,))
    if not row:
        raise CoreError("KEEPSAKE_NOT_FOUND", 404)
    cutoff = after(now, -86400 * settings.raw_event_retention_days)
    ledger.expire_events(store, [row["event_id"]], now, retention_cutoff=cutoff)
    return {"memory_id": memory_id, "state": "RELEASED"}
