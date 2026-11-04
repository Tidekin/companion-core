"""Source-bound projections of the existing extraction call, never new memory facts."""

import hashlib
import json

from companion.contracts import CoreError
from companion.memory_context import MemoryContext
from companion.privacy import redact, sensitive
from companion.store import encode

SUMMARY = "episode_enrichment:"
IMPORTANCE = "memory_importance:"
VERSION = "v3-model-summary"


def _hash(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def _load(store, key):
    row = store.one("SELECT value FROM runtime_meta WHERE key=?", (key,))
    if row:
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            pass
    return None


def _save(store, key, value):
    store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)", (key, encode(value)))


def _settled(store, request_id):
    return store.one("SELECT 1 FROM model_usage WHERE request_id=? "
                     "AND scenario='memory_extract' AND state='SETTLED'", (request_id,))


def _valid(memory, record, now, content_hash):
    try:
        if (record["content_sha256"] != content_hash
            or not _settled(memory.store, record["request_id"])
            or record["source_event_ids"] != record["proof"]["source_event_ids"]):
            return False
        MemoryContext(memory).revalidate(record["proof"], now)
        return True
    except (CoreError, KeyError, TypeError, ValueError):
        return False


def _record(memory, source_ids, request_id, content, now):
    proof = MemoryContext(memory).pin(now, event_ids=source_ids)
    return {"request_id": request_id, "content_sha256": _hash(content), "proof": proof,
            "source_event_ids": proof["source_event_ids"], "at": now}


def summary_groups(memory, events, now):
    """Use the exact original Episode partition, including N2 before late input."""
    text_events = {event["event_id"]: event for event in events
                   if json.loads(event["body_json"]).get("text", "").strip()}
    if not text_events:
        return []
    coverage = memory.episode_coverage(list(text_events), now)
    covered = {key for row in coverage for key in row["source_event_ids"]}
    groups = [row["source_event_ids"] for row in coverage
              if set(row["source_event_ids"]) <= text_events.keys()]
    new = {}
    for key, event in text_events.items():
        if key not in covered:
            new.setdefault(event["conversation_id"], []).append(key)
    return [sorted(group) for group in [*groups, *new.values()]]


def validate_summaries(memory, events, summaries, now):
    groups = {tuple(group) for group in summary_groups(memory, events, now)}
    seen = set()
    for item in summaries:
        ids = set(item.source_event_ids)
        group = tuple(sorted(ids))
        if (group not in groups or group in seen or len(ids) != len(item.source_event_ids)
            or any(sensitive(text) or redact(text, memory.settings) != text
                   for text in (item.title, item.summary))):
            raise CoreError("MEMORY_SUMMARY_SOURCES_INVALID", 502)
        seen.add(group)


def _importance(store, memory_id, records):
    value = sum(record["salience"] for record in records) / (10 * len(records)) if records else 0.5
    store.execute("UPDATE memories SET importance=? WHERE memory_id=? AND importance!=?",
                  (value, memory_id, value))
    if records:
        _save(store, IMPORTANCE + memory_id, records)
    else:
        store.execute("DELETE FROM runtime_meta WHERE key=?", (IMPORTANCE + memory_id,))
    return value


def refresh_importance(memory, row, now):
    """Drop stale estimates before ranking; the original fact status is untouched."""
    saved = _load(memory.store, IMPORTANCE + row["memory_id"])
    if saved is None:
        if row["importance"] != 0.5:
            return _importance(memory.store, row["memory_id"], [])
        return 0.5
    records = [record for record in saved if isinstance(record, dict)
               and type(record.get("salience")) is int and 1 <= record["salience"] <= 10
               and isinstance(record.get("proof"), dict)
               and record["proof"].get("event_ids") == [record.get("event_id")]
               and memory.store.one("SELECT 1 FROM memory_evidence WHERE memory_id=? "
                                    "AND event_id=? AND polarity=1",
                                    (row["memory_id"], record.get("event_id")))
               and _valid(memory, record, now, _hash(row["content"]))] \
        if isinstance(saved, list) and len(saved) <= 20 else []
    value = sum(record["salience"] for record in records) / (10 * len(records)) if records else 0.5
    if records != saved or value != row["importance"]:
        return _importance(memory.store, row["memory_id"], records)
    return value


def record(memory, candidates, memory_ids, events, summaries, request_id, now, *, reference_ids=()):
    """Commit only inside the original source-checked EXTRACT/REVIEW transaction."""
    store = memory.store
    if not store.db.in_transaction:
        raise RuntimeError("MEMORY_ENRICHMENT_REQUIRES_TRANSACTION")
    if not summaries and not any(candidate.salience is not None for candidate in candidates):
        return
    from companion.erasure import cleanup_contracts

    if not any(contract.name == "memory-enrichment" for contract in cleanup_contracts()):
        raise CoreError("MEMORY_ENRICHMENT_ERASURE_REQUIRED", 503)
    if not _settled(store, request_id):
        raise CoreError("MODEL_USAGE_UNRESOLVED", 409)
    validate_summaries(memory, events, summaries, now)
    for candidate, memory_id in zip(candidates, memory_ids, strict=True):
        if candidate.salience is None:
            continue
        row = store.one("SELECT * FROM memories WHERE memory_id=?", (memory_id,))
        if not store.one("SELECT 1 FROM memory_evidence WHERE memory_id=? AND event_id=? "
                          "AND polarity=1", (memory_id, candidate.evidence_event_id)):
            raise CoreError("MEMORY_MATCH_STALE", 409)
        refresh_importance(memory, row, now)
        saved = _load(store, IMPORTANCE + memory_id) or []
        value = _record(memory, [candidate.evidence_event_id], request_id, row["content"], now)
        value.update(event_id=candidate.evidence_event_id, salience=candidate.salience)
        records = [item for item in saved if item.get("event_id") != candidate.evidence_event_id]
        _importance(store, memory_id, [*records, value][-20:])
    for item in summaries:
        ids = sorted(item.source_event_ids)
        matched = [row for row in memory.episode_coverage(ids, now)
                   if sorted(row["source_event_ids"]) == ids]
        # Reuse only the exact original episode, including an earlier N2 checkpoint.
        if len(matched) != 1:
            continue
        episode_id = matched[0]["episode_id"]
        episode = store.one("SELECT * FROM episodes WHERE episode_id=?", (episode_id,))
        ordered = sorted((event for event in events if event["event_id"] in ids),
                         key=lambda event: (event["occurred_at"], event["ingested_at"],
                                            event["event_id"]))
        from companion import memory_references

        extra = {key for reference_id in reference_ids
                 for key in memory_references.read(memory, reference_id, now)["proof"][
                     "source_event_ids"]}
        value = _record(memory, sorted(set(ids) | extra), request_id,
                        {"title": item.title, "summary": item.summary}, now)
        value["event_ids"] = ids
        value["conversation_summary_ids"] = [row["summary_id"] for row in store.rows(
            "SELECT summary_id FROM conversation_summaries WHERE conversation_id=? "
            "AND first_event_id=? AND last_event_id=?", (episode["conversation_id"],
                ordered[0]["event_id"], ordered[-1]["event_id"]))]
        store.execute("UPDATE episodes SET title=?,summary=?,extractor_version=? "
                      "WHERE episode_id=?", (item.title, item.summary, VERSION, episode_id))
        store.execute("UPDATE conversation_summaries SET summary_text=?,summarizer_version=? "
                      "WHERE conversation_id=? AND first_event_id=? AND last_event_id=?",
                      (item.summary, VERSION, episode["conversation_id"],
                       ordered[0]["event_id"], ordered[-1]["event_id"]))
        _save(store, SUMMARY + episode_id, value)


def summary(memory, episode_id, source_event_ids, now):
    value = _load(memory.store, SUMMARY + episode_id)
    row = memory.store.one("SELECT title,summary,extractor_version,status FROM episodes "
                            "WHERE episode_id=?", (episode_id,))
    actual_ids = [item["event_id"] for item in memory.store.rows(
        "SELECT event_id FROM episode_events WHERE episode_id=? ORDER BY event_id LIMIT 21",
        (episode_id,))]
    if (not isinstance(value, dict) or not row or row["status"] != "ACTIVE"
        or row["extractor_version"] != VERSION
        or value.get("event_ids") != sorted(source_event_ids)
        or actual_ids != sorted(source_event_ids)
        or not _valid(memory, value, now, _hash({
            "title": row["title"], "summary": row["summary"]}))):
        return None
    return {"kind": "model_summary", "title": row["title"], "text": row["summary"],
            "request_id": value["request_id"]}


def erase(store, event_ids):
    """Native erasure/replay removes derived material without touching original costs."""
    affected = set(event_ids)
    for row in store.rows("SELECT key,value FROM runtime_meta WHERE substr(key,1,?)=? "
                          "OR substr(key,1,?)=?", (len(SUMMARY), SUMMARY,
                                                   len(IMPORTANCE), IMPORTANCE)):
        value = None
        if row["key"].startswith(IMPORTANCE):
            memory_id = row["key"][len(IMPORTANCE):]
            if not store.one("SELECT 1 FROM memories WHERE memory_id=? AND status!='FORGOTTEN'",
                              (memory_id,)):
                _importance(store, memory_id, [])
                continue
        try:
            value = json.loads(row["value"])
            if row["key"].startswith(SUMMARY):
                if not affected.intersection(value["source_event_ids"]):
                    continue
            else:
                records = [item for item in value
                           if not affected.intersection(item["source_event_ids"])]
                if records == value:
                    continue
                _importance(store, row["key"][len(IMPORTANCE):], records)
                continue
        except (ValueError, TypeError, KeyError):
            pass
        store.execute("DELETE FROM runtime_meta WHERE key=?", (row["key"],))
        if row["key"].startswith(SUMMARY):
            store.execute("UPDATE episodes SET title='',summary='',status='REDACTED' "
                          "WHERE episode_id=?", (row["key"][len(SUMMARY):],))
            ids = value.get("conversation_summary_ids", []) if isinstance(value, dict) else []
            store.execute("DELETE FROM conversation_summaries WHERE summary_id IN "
                          "(SELECT value FROM json_each(?))", (encode(ids),))
        else:
            _importance(store, row["key"][len(IMPORTANCE):], [])
