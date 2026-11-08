"""Offline candidate experiment; no production retrieval hook or persistent graph.

All candidates come from the same native Memory/PostgreSQL evidence pool. An alias
annotation is a synthetic human-labelled assertion, not automatically proven
semantics. Its exact owner source and quote must still be present at query time.
"""

import hashlib
import json
import re
import unicodedata

from companion import live_text
from companion.contracts import CoreError
from companion.memory_candidates import terms
from companion.observation import expired_sources
from companion.privacy import epoch
from companion.store import after

LIMIT = 6
MAX_ADDITIONS = 2
MAX_POOL = 200


def normalized(value):
    return unicodedata.normalize("NFKC", value).casefold()


def mentions(text, phrase):
    text, phrase = normalized(text), normalized(phrase)
    if re.fullmatch(r"[a-z0-9_]+", phrase):
        return bool(re.search(r"(?<![a-z0-9_])" + re.escape(phrase) + r"(?![a-z0-9_])", text))
    return phrase in text


def evidence_pool(memory, now):
    rows = memory.search("*", subjects=("user", "project"), limit=MAX_POOL + 1)
    if len(rows) > MAX_POOL:
        raise ValueError("RESEARCH_POOL_LIMIT")
    cutoff = after(now, -86400 * memory.settings.raw_event_retention_days)
    sources = memory.store.rows(
        "SELECT e.* FROM events e JOIN conversations c USING(conversation_id) "
        "WHERE e.redacted=0 AND e.actor_id='owner' AND e.source='qq.onebot' "
        "AND e.privacy_scope='private' AND c.scope='private' AND c.owner_actor_id='owner' "
        "AND e.ingested_at>=%s AND e.occurred_at<=%s", (cutoff, now),
    )
    valid = {}
    for source in sources:
        if not expired_sources(memory.store, [source["event_id"]], cutoff):
            text = json.loads(source["body_json"]).get("text", "")
            if isinstance(text, str) and text:
                valid[source["event_id"]] = {"text": text,
                    "sha256": hashlib.sha256(text.encode()).hexdigest()}
    eligible = []
    for row in rows:
        sources = set(row["evidence_ids"]) & valid.keys()
        if row["status"] == "CONFIRMED" and not row["needs_review"] and sources:
            eligible.append({**row, "research_sources": sorted(sources)})
    return eligible, valid


def lexical_ids(store, query, rows):
    """The native lexical branch before BGE filtering/fusion; same six-candidate cap."""
    if len(query) < 3:
        return [row["memory_id"] for row in rows if query in row["content"]][:LIMIT]
    patterns = ["%" + term.replace("\\", "\\\\").replace("%", "\\%").replace(
        "_", "\\_") + "%" for term in terms(query)]
    allowed = [row["memory_id"] for row in rows]
    return [row["memory_id"] for row in store.rows(
        "SELECT memory_id FROM memories WHERE memory_id=ANY(%s::text[]) "
        "AND content ILIKE ANY(%s::text[]) "
        "ORDER BY public.word_similarity(%s,content) DESC,updated_at DESC,memory_id LIMIT %s",
        (allowed, patterns, query, LIMIT),
    )] if patterns and allowed else []


def active_annotations(annotations, rows, sources):
    active = []
    for item in annotations:
        if (set(item) != {"canonical", "aliases", "subject", "source_event_id",
                          "source_sha256", "evidence_quote"}
            or item["subject"] not in {"user", "project"}
            or not isinstance(item["aliases"], list) or not 1 <= len(item["aliases"]) <= 4
            or not all(isinstance(word, str) and 2 <= len(word) <= 80
                       for word in [item["canonical"], *item["aliases"]])):
            raise ValueError("INVALID_RESEARCH_ANNOTATION")
        proof = sources.get(item["source_event_id"])
        if proof is None or proof["sha256"] != item["source_sha256"]:
            continue
        quote = item["evidence_quote"]
        if (not isinstance(quote, str) or quote not in proof["text"]
            or not all(mentions(quote, word) for word in [item["canonical"], *item["aliases"]])):
            raise ValueError("UNSUPPORTED_RESEARCH_ANNOTATION")
        members = {row["memory_id"] for row in rows
                   if row["subject"] == item["subject"]
                   and mentions(row["content"], item["canonical"])}
        active.append((item, members))
    return active


def add_candidates(query, rows, sources, seeds, annotations):
    """Add at most two candidates; neighbors never become new traversal roots."""
    by_id = {row["memory_id"]: row for row in rows}
    selected = list(dict.fromkeys(key for key in seeds if key in by_id))[:LIMIT]
    starting_count = len(selected)
    additions = []
    ordered = sorted(rows, key=lambda row: (row["content"], row["subject"]))
    active = active_annotations(annotations, rows, sources)

    def add(key, kind, item, shared=()):
        if key in selected or len(selected) >= LIMIT or len(additions) >= MAX_ADDITIONS:
            return
        selected.append(key)
        additions.append({"memory_id": key, "kind": kind,
            "annotation_source_sha256": item["source_sha256"],
            "edge_source_sha256": [sources[source]["sha256"] for source in sorted(shared)]})

    # Exact, source-labelled aliases only. No query rewriting or gold-label lookup.
    for item, members in active:
        if any(mentions(query, alias) for alias in [item["canonical"], *item["aliases"]]):
            for row in ordered:
                if row["memory_id"] in members:
                    add(row["memory_id"], "explicit_alias", item)
    roots = list(selected)
    for root in roots:
        for item, members in active:
            if root not in members:
                continue
            for row in ordered:
                key = row["memory_id"]
                shared = set(row["research_sources"]) & set(by_id[root]["research_sources"])
                # A common label alone is insufficient: the same owner source must contain
                # both literal facts. This is candidate co-reference, never a new fact.
                supported = {
                    source for source in shared if row["content"] in sources[source]["text"]
                    and by_id[root]["content"] in sources[source]["text"]
                }
                if key in members and supported:
                    add(key, "one_hop_same_owner_source", item, supported)
    assert len(selected) <= LIMIT and len(selected) - starting_count <= MAX_ADDITIONS
    return selected, additions


def retrieve(memory, query, method, annotations, now, *, seeds=None):
    """Only local experiment code calls this; use original guards around derived work."""
    captured = epoch(memory.store)
    custody = live_text._ledger_custody(memory.store, memory.settings)
    rows, sources = evidence_pool(memory, now)
    if method == "fts":
        ids, additions = lexical_ids(memory.store, query, rows), []
    elif method in {"fts_bge", "bounded_tags_onehop"}:
        ids = [row["memory_id"] for row in memory.search(
            query, subjects=("user", "project"), limit=LIMIT,
            memory_ids=[row["memory_id"] for row in rows],
        )] if seeds is None else list(seeds)
        additions = []
        if method == "bounded_tags_onehop":
            ids, additions = add_candidates(query, rows, sources, ids, annotations)
    else:
        raise ValueError("UNKNOWN_RESEARCH_METHOD")
    live_text._ledger_custody(memory.store, memory.settings, custody)
    if captured != epoch(memory.store):
        raise CoreError("PRIVACY_CHANGED", 409)
    by_id = {row["memory_id"]: row for row in rows}
    return [by_id[key] for key in ids if key in by_id], additions
