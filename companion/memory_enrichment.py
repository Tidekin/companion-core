"""Source-bound projections of the existing extraction call, never new memory facts."""

import hashlib
import json

from companion import retrieval_work
from companion.contracts import CoreError
from companion.memory_context import MemoryContext
from companion.privacy import redact, sensitive
from companion.store import encode

SUMMARY = "episode_enrichment:"
IMPORTANCE = "memory_importance:"
VERSION = "v3-model-summary"


def _hash(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def _load(store, key, *, work=None):
    if work is not None:
        size = work.one(store, 'SELECT OCTET_LENGTH(value) AS size FROM runtime_meta '
                        'WHERE key = %s',
                         (key,))
        if not size:
            return None
        if not work.take("text_bytes", size["size"]):
            raise retrieval_work.WorkLimit("enrichment_text_bytes")
        row = work.one(store, 'SELECT value FROM runtime_meta WHERE key = %s '
                       'AND OCTET_LENGTH(value) = %s',
                        (key, size["size"]))
    else:
        row = store.one('SELECT value FROM runtime_meta WHERE key = %s', (key,))
    if row:
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            pass
    return None


def _save(store, key, value):
    store.execute('INSERT INTO runtime_meta (key, value) VALUES (%s, %s) ON CONFLICT(key) DO '
        'UPDATE SET value = excluded.value, record_sequence = DEFAULT', (key, encode(value)))


def _recorded_usage(store, request_id):
    return store.one('SELECT 1 FROM model_usage WHERE request_id = %s AND scenario = '
        "'memory_extract' AND state IN ('SETTLED','UNKNOWN')", (request_id,))


def _valid(memory, record, now, content_hash, *, work=None, source_view=None, recorded_ids=None):
    try:
        if (record["content_sha256"] != content_hash
            or not (_recorded_usage(memory.store, record["request_id"]) if recorded_ids is None
                    else record["request_id"] in recorded_ids)
            or record["source_event_ids"] != record["proof"]["source_event_ids"]):
            return False
        MemoryContext(memory).revalidate(record["proof"], now, work=work, source_view=source_view)
        return True
    except retrieval_work.WorkLimit:
        raise
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
    store.execute('UPDATE memories SET importance = %s WHERE memory_id = %s AND importance <> %s',
                  (value, memory_id, value))
    if records:
        _save(store, IMPORTANCE + memory_id, records)
    else:
        store.execute('DELETE FROM runtime_meta WHERE key = %s', (IMPORTANCE + memory_id,))
    return value


def refresh_importance(memory, row, now):
    """Drop stale estimates before ranking; the original fact status is untouched."""
    saved = _load(memory.store, IMPORTANCE + row["memory_id"], work=retrieval_work.current())
    if saved is None:
        if row["importance"] != 0.5:
            return _importance(memory.store, row["memory_id"], [])
        return 0.5
    records = [record for record in saved if isinstance(record, dict)
               and type(record.get("salience")) is int and 1 <= record["salience"] <= 10
               and isinstance(record.get("proof"), dict)
               and record["proof"].get("event_ids") == [record.get("event_id")]
               and memory.store.one('SELECT 1 FROM memory_evidence WHERE memory_id = %s AND '
                   'event_id = %s AND polarity = 1',
                                    (row["memory_id"], record.get("event_id")))
               and _valid(memory, record, now, _hash(row["content"]),
                          work=retrieval_work.current())] \
        if isinstance(saved, list) and len(saved) <= 20 else []
    value = sum(record["salience"] for record in records) / (10 * len(records)) if records else 0.5
    if records != saved or value != row["importance"]:
        return _importance(memory.store, row["memory_id"], records)
    return value


def ranking_importance(memory, rows, now, *, sources):
    """Bounded ranking projection; facts still require their original full proof.

    Read metadata, positive support and extraction receipts in batches. Reuse only
    this immutable snapshot's original sources, never a permission verdict. Missing
    or unaffordable derived scores use the established neutral .5 value.
    """
    if not memory.store.in_read_snapshot or not sources._snapshot_cache():
        raise RuntimeError("MEMORY_SOURCE_SNAPSHOT_REQUIRED")
    work, store = sources.work, memory.store
    scores = {row["memory_id"]: .5 for row in rows}
    if not rows:
        return scores
    try:
        keys = [IMPORTANCE + row["memory_id"] for row in rows]
        sizes = work.source_rows(store,
            "SELECT key,octet_length(value) AS size FROM runtime_meta "
            "WHERE key=ANY(%s::text[])", (keys,), limit=len(keys))
        # Ranking metadata cannot spend the bytes reserved for actual facts and
        # their next-boundary source checks. It is never evidence to display.
        allowance = min(65536, work.remaining("text_bytes") // 8)
        admitted = []
        for item in sizes:
            if item["size"] <= allowance and work.take("text_bytes", item["size"]):
                admitted.append(item)
                allowance -= item["size"]
            else:
                work.mark("importance_metadata")
        if not admitted:
            return scores
        values = work.source_rows(store,
            "SELECT r.key,r.value FROM runtime_meta r JOIN "
            "unnest(%s::text[],%s::bigint[]) expected(key,bytes) USING(key) "
            "WHERE octet_length(r.value)=expected.bytes",
            ([r["key"] for r in admitted], [r["size"] for r in admitted]), limit=len(admitted))
        saved = {}
        for value in values:
            try:
                records = json.loads(value["value"])
            except (ValueError, TypeError):
                continue
            if isinstance(records, list) and len(records) <= 20:
                saved[value["key"][len(IMPORTANCE):]] = [record for record in records
                    if isinstance(record, dict) and type(record.get("salience")) is int
                    and 1 <= record["salience"] <= 10 and isinstance(record.get("proof"), dict)
                    and record["proof"].get("event_ids") == [record.get("event_id")]
                    and isinstance(record.get("event_id"), str)
                    and isinstance(record.get("request_id"), str)]
        pairs = [(mid, r["event_id"]) for mid, records in saved.items() for r in records]
        requests = list({r["request_id"] for records in saved.values() for r in records})
        if not pairs or not requests:
            return scores
        supported = {(r["memory_id"], r["event_id"]) for r in work.source_rows(store,
            "SELECT v.memory_id,v.event_id FROM memory_evidence v JOIN "
            "unnest(%s::text[],%s::text[]) wanted(memory_id,event_id) "
            "USING(memory_id,event_id) WHERE v.polarity=1",
            ([p[0] for p in pairs], [p[1] for p in pairs]), limit=len(pairs))}
        recorded = {r["request_id"] for r in work.source_rows(store,
            "SELECT request_id FROM model_usage WHERE request_id=ANY(%s::text[]) "
            "AND scenario='memory_extract' AND state IN ('SETTLED','UNKNOWN')",
            (requests,), limit=len(requests))}
        for row in rows:
            valid = [record for record in saved.get(row["memory_id"], [])
                if (row["memory_id"], record["event_id"]) in supported
                and _valid(memory, record, now, _hash(row["content"]), work=work,
                           source_view=sources, recorded_ids=recorded)]
            if valid:
                scores[row["memory_id"]] = sum(r["salience"] for r in valid)/(10*len(valid))
    except retrieval_work.WorkLimit:
        work.mark("importance_metadata")
    return scores


def record(memory, candidates, memory_ids, events, summaries, request_id, now, *, reference_ids=()):
    """Commit only inside the original source-checked EXTRACT/REVIEW transaction."""
    store = memory.store
    if not store.in_transaction:
        raise RuntimeError("MEMORY_ENRICHMENT_REQUIRES_TRANSACTION")
    if not summaries and not any(candidate.salience is not None for candidate in candidates):
        return
    from companion.erasure import cleanup_contracts

    if not any(contract.name == "memory-enrichment" for contract in cleanup_contracts()):
        raise CoreError("MEMORY_ENRICHMENT_ERASURE_REQUIRED", 503)
    if not _recorded_usage(store, request_id):
        raise CoreError("MODEL_USAGE_UNRESOLVED", 409)
    validate_summaries(memory, events, summaries, now)
    for candidate, memory_id in zip(candidates, memory_ids, strict=True):
        if candidate.salience is None:
            continue
        row = store.one('SELECT memories.memory_id AS memory_id, memories.subject AS subject, '
            'memories.kind AS kind, memories.content AS content, memories.status AS status, '
            'memories.origin AS origin, memories.confidence AS confidence, memories.importance AS '
            'importance, memories.privacy_scope AS privacy_scope, memories.supersedes_memory_id AS '
            'supersedes_memory_id, memories.created_at AS created_at, memories.updated_at AS '
            'updated_at, memories.valid_from AS valid_from, memories.valid_to AS valid_to, '
            'memories.last_reviewed_at AS last_reviewed_at, memories.fact_key AS fact_key, '
            'memories.needs_review AS needs_review FROM memories AS memories WHERE '
            'memories.memory_id = %s', (memory_id,))
        if not store.one('SELECT 1 FROM memory_evidence WHERE memory_id = %s AND event_id = %s AND '
            'polarity = 1', (memory_id, candidate.evidence_event_id)):
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
        episode = store.one('SELECT episodes.episode_id AS episode_id, episodes.title AS title, '
            'episodes.summary AS summary, episodes.conversation_id AS conversation_id, '
            'episodes.privacy_scope AS privacy_scope, episodes.started_at AS started_at, '
            'episodes.ended_at AS ended_at, episodes.status AS status, episodes.extractor_version '
            'AS extractor_version, episodes.created_at AS created_at FROM episodes AS episodes '
            'WHERE episodes.episode_id = %s', (episode_id,))
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
            'SELECT summary_id FROM conversation_summaries WHERE conversation_id = %s AND '
            'first_event_id = %s AND last_event_id = %s', (episode["conversation_id"],
                ordered[0]["event_id"], ordered[-1]["event_id"]))]
        store.execute('UPDATE episodes SET title = %s, summary = %s, extractor_version = %s WHERE '
            'episode_id = %s', (item.title, item.summary, VERSION, episode_id))
        store.execute('UPDATE conversation_summaries SET summary_text = %s, summarizer_version = '
            '%s WHERE conversation_id = %s AND first_event_id = %s AND last_event_id = %s',
                      (item.summary, VERSION, episode["conversation_id"],
                       ordered[0]["event_id"], ordered[-1]["event_id"]))
        _save(store, SUMMARY + episode_id, value)


def summary(memory, episode_id, source_event_ids, now):
    work = retrieval_work.current()
    value = _load(memory.store, SUMMARY + episode_id, work=work)
    row = memory.store.one('SELECT title, summary, extractor_version, status FROM episodes WHERE '
        'episode_id = %s', (episode_id,))
    actual_ids = [item["event_id"] for item in memory.store.rows(
        'SELECT event_id FROM episode_events WHERE episode_id = %s ORDER BY event_id NULLS FIRST '
        'LIMIT 21',
        (episode_id,))]
    if (not isinstance(value, dict) or not row or row["status"] != "ACTIVE"
        or row["extractor_version"] != VERSION
        or value.get("event_ids") != sorted(source_event_ids)
        or actual_ids != sorted(source_event_ids)
        or not _valid(memory, value, now, _hash({
            "title": row["title"], "summary": row["summary"]}), work=work)):
        return None
    return {"kind": "model_summary", "title": row["title"], "text": row["summary"],
            "request_id": value["request_id"]}


def erase(store, event_ids):
    """Native erasure/replay removes derived material without touching original costs."""
    affected = set(event_ids)
    for row in store.rows('SELECT key, value FROM runtime_meta WHERE SUBSTRING(key FROM 1 FOR %s) '
        '= %s OR SUBSTRING(key FROM 1 FOR %s) = %s', (len(SUMMARY), SUMMARY,
                                                   len(IMPORTANCE), IMPORTANCE)):
        value = None
        if row["key"].startswith(IMPORTANCE):
            memory_id = row["key"][len(IMPORTANCE):]
            if not store.one('SELECT 1 FROM memories WHERE memory_id = %s AND status <> '
                "'FORGOTTEN'",
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
        store.execute('DELETE FROM runtime_meta WHERE key = %s', (row["key"],))
        if row["key"].startswith(SUMMARY):
            store.execute("UPDATE episodes SET title = '', summary = '', status = 'REDACTED' WHERE "
                'episode_id = %s', (row["key"][len(SUMMARY):],))
            ids = value.get("conversation_summary_ids", []) if isinstance(value, dict) else []
            store.execute('DELETE FROM conversation_summaries WHERE summary_id IN (SELECT value '
                'COLLATE "C" FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS '
                'parsed(value))', (encode(ids),))
        else:
            _importance(store, row["key"][len(IMPORTANCE):], [])
