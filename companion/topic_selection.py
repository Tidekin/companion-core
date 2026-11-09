"""Interest-driven discovery: choose what a discover request searches for.

Owner decisions 2026-10-29 (`.local/design-drafts/interest-topic-selection.md`): 60% of
discover runs search one interest, 40% keep the original discovery; her own interests
come from the watch ledger, and the owner's confirmed private preferences may be used
as search terms (the term then goes to the platform under her account). Heybox keeps
its fixed topic rotation for now; a source joins by registering in QUERY_SOURCES.

No model call and no new state: interests are a projection over the watch ledger and
memories, and rotation reads earlier discover requests.
"""

import json
import math
import random
import re
from datetime import datetime

from companion import owner_authority
from companion.privacy import redact, sensitive
from companion.store import after

INTEREST_SHARE = 0.6
WINDOW_DAYS = 30
HALF_LIFE_DAYS = 7
MAX_TOPICS = 8
MAX_OWNER_TERMS = 5
OWNER_WEIGHT = 1.0
ROTATION_HOURS = 24
# Sources whose Reader accepts a discover `query`; Heybox can register later.
QUERY_SOURCES = {"bilibili"}
PREFERENCE_PREFIX = re.compile(
    r"^(?:主人|我|用户)?(?:很|非常|比较|最近|一直)?(?:喜欢|爱看|爱玩|在玩|在看|关注|想看|想玩|"
    r"迷上了|沉迷|喜爱)(?:看|玩|听)?")


def clean(term):
    """A search term is 2-12 letters, digits or spaces; anything else is not searched."""
    if not isinstance(term, str):
        return None
    value = re.sub(r"\s+", " ", re.sub(r"[^\w\s]|_", " ", term)).strip()
    if (not 2 <= len(value) <= 12 or re.search(r"https?|www|\d{5,}", value, re.I)
            or sensitive(value)):
        return None
    return value


def interest_topics(store, now):
    """Her own topics from judged watch entries: (rating + 1) with a 7-day half-life."""
    since = after(now, -WINDOW_DAYS * 86400)
    reference = datetime.fromisoformat(now)
    scores = {}
    for row in store.rows(
        'SELECT w.rating, w.topic_tags_json, w.updated_at FROM watch_ledger AS w JOIN events AS e '
        'ON e.event_id = w.source_event_id JOIN observations AS o ON o.event_id = '
        "w.source_event_id WHERE w.state = 'JUDGED' AND e.redacted = 0 AND o.redacted = 0 AND "
        'w.updated_at >= %s',
        (since,),
    ):
        age = (reference - datetime.fromisoformat(row["updated_at"])).total_seconds() / 86400
        weight = (row["rating"] + 1) * math.pow(0.5, max(age, 0) / HALF_LIFE_DAYS)
        for tag in json.loads(row["topic_tags_json"]):
            if term := clean(tag):
                scores[term] = scores.get(term, 0) + weight
    ranked = sorted(((t, s) for t, s in scores.items() if s > 0), key=lambda x: (-x[1], x[0]))
    return ranked[:MAX_TOPICS]


def owner_terms(store, settings, now):
    """Short terms from the owner's confirmed private preferences, when memory is allowed."""
    from companion.interest import profile

    if owner_authority.required(store, settings):
        try:
            owner_authority.current(store, settings, lambda: now, purpose="memory")
        except Exception:
            return []
    terms = []
    for row in profile(store, settings, now, settings.owner_qq_id)["owner.preference"]:
        term = clean(PREFERENCE_PREFIX.sub("", row["content"].strip()))
        if term and redact(term, settings) == term and term not in terms:
            terms.append(term)
        if len(terms) == MAX_OWNER_TERMS:
            break
    return terms


def recent_queries(store, now):
    since = after(now, -ROTATION_HOURS * 3600)
    return {row["query"] for row in store.rows(
        "SELECT query FROM exploration_requests WHERE operation = 'discover' AND NOT query IS NULL "
        'AND created_at >= %s', (since,))}


def choose(store, settings, now, adapters, rng=None, *, own_only=False):
    """Return (adapter_id, query) for one discover run, or None for the original rotation."""
    rng = rng or random  # Looked up per call, so tests can substitute the module's source.
    searchable = [name for name in adapters
                  if settings.observation_adapters[name].source in QUERY_SOURCES]
    if not searchable or (not own_only and rng.random() >= INTEREST_SHARE):
        return None
    used = recent_queries(store, now)
    candidates = [(term, score) for term, score in interest_topics(store, now)
                  if term not in used]
    if not own_only:
        candidates += [(term, OWNER_WEIGHT) for term in owner_terms(store, settings, now)
                       if term not in used and term not in {t for t, _ in candidates}]
    if not candidates:
        return None
    total = sum(score for _, score in candidates)
    pick, acc = rng.random() * total, 0.0
    for term, score in candidates:
        acc += score
        if pick < acc:
            return searchable[0], term
    return searchable[0], candidates[-1][0]
