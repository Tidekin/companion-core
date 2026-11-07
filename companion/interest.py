"""Self interests and views rebuilt from actual completed reads and her own judgements.

Each independent completed self read is one experience; a judged Watch Ledger entry for the
same item is her local view of it. Interest engagement counts only reads she chose herself; a
link the owner sent is a starting point whose judgement still shapes her view. Views replay
the latest judged experiences in reading order, so new evidence can strengthen, weaken or
revise them, and each change keeps the experience that caused it. Nothing here is stored:
source erasure, expiry or an owner-withdrawn judgement changes the next rebuild, and a
restored backup replays the erasure ledger before it is read. Plans, Life choices and model
prose never count as experiences.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from companion.contracts import CoreError
from companion.observation import expired_sources
from companion.privacy import epoch, redact, sensitive
from companion.store import after, uid

FADE_DAYS = 30  # No new self-chosen read for this long: the interest rests; nothing is erased.
VIEW_WINDOW = 5  # Only the latest judged experiences form the current view.
SETTLED_EXPERIENCES, SETTLED_DAYS = 3, 2  # One judgement is never more than tentative.
HISTORY_LIMIT = 8
STANCES = {"positive": "喜欢", "negative": "不太喜欢", "mixed": "看法有分歧",
           "neutral": "感觉一般", "undecided": "还没形成看法"}
CERTAINTIES = {"tentative": "暂时的", "settled": "比较稳定的"}


def read_origin(store, goal_id, urls):
    """A self goal whose target the owner sent is her starting point, not her own pick."""
    urls = {url.rstrip("/") for url in urls if url}
    for row in store.rows(
        "SELECT e.body_json FROM goal_evidence ge JOIN events e USING(event_id) "
        "WHERE ge.goal_id=? AND e.actor_id='owner' AND e.redacted=0", (goal_id,),
    ):
        body = json.loads(row["body_json"])
        said = " ".join(str(body.get(key) or "") for key in ("text", "url"))
        if any(url in said for url in urls):
            return "owner_suggested"
    return "self"


def judgement(store, settings, event_id, item_ref, cutoff):
    """Her live judgement of this read, or of a later deeper read of the same item."""
    if not store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watch_ledger'"):
        return None
    entry = store.one(
        "SELECT w.entry_id,w.source_event_id,w.rating,w.take,w.updated_at "
        "FROM watch_ledger w JOIN events e ON e.event_id=w.source_event_id "
        "JOIN observations o ON o.event_id=w.source_event_id WHERE w.state='JUDGED' "
        "AND e.redacted=0 AND o.redacted=0 AND (w.source_event_id=? OR w.item_ref=?) "
        "ORDER BY w.updated_at DESC LIMIT 1", (event_id, item_ref),
    )
    if (not entry or expired_sources(store, [entry["source_event_id"]], cutoff)
            or sensitive(entry["take"]) or redact(entry["take"], settings) != entry["take"]):
        return None
    return {"entry_id": entry["entry_id"], "source_event_id": entry["source_event_id"],
            "rating": entry["rating"], "take": entry["take"],
            "judged_at": entry["updated_at"]}


def exploration_evidence(store, settings, trait_id, now):
    """Only completed self goals count; repeats never add another independent experience."""
    cutoff = after(now, -86400 * settings.raw_event_retention_days)
    rows = store.rows(
        "SELECT e.event_id,e.occurred_at,e.source,o.source_sha256,o.canonical_url,"
        "o.result_status,o.evidence_json,(SELECT MIN(g.goal_id) FROM goals g "
        "WHERE g.kind='self' AND g.state='DONE' AND g.redacted=0 "
        "AND g.result_event_id=e.event_id AND substr(g.topic,1,80)=tr.topic) AS goal_id "
        "FROM trait_evidence t JOIN observations o USING(event_id) "
        "JOIN events e USING(event_id) JOIN traits tr USING(trait_id) "
        "WHERE t.trait_id=? AND tr.category='interest' AND o.redacted=0 AND e.redacted=0 "
        "AND e.ingested_at>=? AND e.occurred_at<=? AND o.result_status IN ('FETCHED','WATCHED') "
        "AND o.source_sha256<>'' AND o.canonical_url<>'' AND EXISTS (SELECT 1 FROM goals g "
        "WHERE g.kind='self' AND g.state='DONE' AND g.redacted=0 "
        "AND g.result_event_id=e.event_id AND substr(g.topic,1,80)=tr.topic) "
        "ORDER BY e.occurred_at,e.event_id",
        (trait_id, cutoff, now),
    )
    evidence, hashes, urls = [], set(), set()
    for row in rows:
        simulated = row["source"] == "observation.simulation"
        if simulated and settings.mode != "demo":
            continue
        if row["source_sha256"] in hashes or row["canonical_url"] in urls:
            continue
        if expired_sources(store, [row["event_id"]], cutoff):
            continue
        hashes.add(row["source_sha256"])
        urls.add(row["canonical_url"])
        requested = json.loads(row.pop("evidence_json") or "{}").get("requested_url", "")
        evidence.append({
            **row, "evidence_level": ("SIMULATED_" if simulated else "") + row["result_status"],
            "origin": read_origin(store, row["goal_id"], [row["canonical_url"], requested]),
            "judgement": judgement(store, settings, row["event_id"], row["canonical_url"],
                                   cutoff),
        })
    return evidence


def _local_day(value, settings):
    return datetime.fromisoformat(value).astimezone(ZoneInfo(settings.timezone)).date()


def _stance(window, settings):
    ratings = [row["judgement"]["rating"] for row in window]
    mean = sum(ratings) / len(ratings)
    stance = ("mixed" if max(ratings) >= 1 and min(ratings) <= -1 and abs(mean) < 1
              else "positive" if mean >= 0.75 else "negative" if mean <= -0.75 else "neutral")
    days = {_local_day(row["occurred_at"], settings) for row in window}
    settled = len(window) >= SETTLED_EXPERIENCES and len(days) >= SETTLED_DAYS
    return stance, "settled" if settled else "tentative"


def _experience(row):
    return {"event_id": row["event_id"], "occurred_at": row["occurred_at"],
            "source_event_id": row["judgement"]["source_event_id"],
            "entry_id": row["judgement"]["entry_id"],
            "judged_at": row["judgement"]["judged_at"],
            "origin": row.get("origin", "self"), "rating": row["judgement"]["rating"],
            "take": row["judgement"]["take"]}


def view(evidence, settings):
    """Replay judged experiences in reading order; every change names its cause."""
    judged = [row for row in evidence if row.get("judgement")]
    stance, certainty, history = "undecided", "tentative", []
    for index, row in enumerate(judged):
        current = _stance(judged[max(0, index + 1 - VIEW_WINDOW):index + 1], settings)
        if current != (stance, certainty):
            history.append({
                "at": row["judgement"]["judged_at"],
                "from": None if stance == "undecided" else stance,
                "from_certainty": None if stance == "undecided" else certainty,
                "to": current[0], "to_certainty": current[1], "because": _experience(row),
            })
            stance, certainty = current
    return {"stance": stance, "certainty": certainty,
            "judged_experiences": len(judged),
            "unjudged_experiences": len(evidence) - len(judged),
            "latest_unjudged": bool(evidence) and not evidence[-1].get("judgement"),
            "latest": [_experience(row) for row in judged[-3:]],
            "history": history[-HISTORY_LIMIT:]}


def tendency(evidence, settings, now=None):
    """Engagement from her own picks; a settled dislike or a long rest lets it fade."""
    own = [row for row in evidence if row.get("origin", "self") == "self"]
    days = {_local_day(row["occurred_at"], settings) for row in own}
    state = "EXPLORING" if evidence else "FADING"
    if len(own) >= settings.emerging_min_sessions:
        state = "STABLE" if len(days) >= settings.stable_min_days else "EMERGING"
    current = view(evidence, settings)
    if (current["stance"], current["certainty"]) == ("negative", "settled") or (
        now and own and own[-1]["occurred_at"] < after(now, -FADE_DAYS * 86400)
    ):
        state = "FADING"
    return state, len(own), len(days)


def explain(item):
    """Why she follows a topic and why her view changed, from the rebuilt evidence only."""
    current, suggested = item["view"], item.get("owner_suggested_sessions", 0)
    lines = [
        f"「{item['topic']}」：自己挑着看了{item['independent_sessions']}次"
        f"（{item['independent_days']}天）"
        + (f"，另有{suggested}次是主人提到后去看的" if suggested else "")
        + f"；现在是{CERTAINTIES[current['certainty']]}看法：{STANCES[current['stance']]}。"
    ]
    for change in current["history"][-3:]:
        cause = change["because"]
        if change["from"] is None:
            moved = f"开始觉得{STANCES[change['to']]}"
        elif change["from"] == change["to"]:
            moved = f"“{STANCES[change['to']]}”变成了{CERTAINTIES[change['to_certainty']]}看法"
        else:
            moved = f"从{STANCES[change['from']]}变成{STANCES[change['to']]}"
        lines.append(f"  {change['at'][:10]} {moved}，因为 {cause['occurred_at'][:10]} "
                     f"读的那次觉得：{cause['take']}")
    if not current["history"]:
        lines.append("  读过，但还没形成自己的评价。")
    lines.append("  依据的内容记录：" + "；".join(
        f"{row['occurred_at'][:10]} {row['canonical_url']}" for row in item["evidence"][-3:]))
    return lines


def profile(store, settings, now, requester):
    """Private, rebuilt evidence view. Neither namespace grants execution or sharing."""
    if requester != settings.owner_qq_id:
        raise CoreError("OWNER_REQUIRED", 403)
    captured = epoch(store)
    owner = []
    cutoff = after(now, -86400 * settings.raw_event_retention_days)
    for row in store.rows(
        "SELECT memory_id,content,fact_key FROM memories WHERE subject='user' "
        "AND kind='preference' AND status='CONFIRMED' AND needs_review=0 AND confidence>0 "
        "AND privacy_scope='private' ORDER BY updated_at DESC,memory_id LIMIT 40"
    ):
        evidence = store.rows(
            "SELECT e.event_id,e.source,e.occurred_at FROM memory_evidence me "
            "JOIN events e USING(event_id) JOIN conversations c USING(conversation_id) "
            "WHERE me.memory_id=? AND e.redacted=0 AND e.actor_id='owner' "
            "AND e.privacy_scope='private' AND c.scope='private' AND c.owner_actor_id='owner' "
            "AND e.ingested_at>=? AND e.occurred_at<=? AND e.source IN "
            "('qq.onebot','owner.decision') ORDER BY e.occurred_at,e.event_id",
            (row["memory_id"], cutoff, now),
        )
        if evidence and not expired_sources(store, [e["event_id"] for e in evidence], cutoff):
            if not sensitive(row["content"]) and redact(row["content"], settings) == row["content"]:
                owner.append({**row, "namespace": "owner.preference", "evidence": evidence})
    interests = []
    for row in store.rows(
        "SELECT trait_id,topic FROM traits WHERE category='interest' "
        "ORDER BY updated_at DESC,trait_id LIMIT 40"
    ):
        evidence = exploration_evidence(store, settings, row["trait_id"], now)
        state, sessions, days = tendency(evidence, settings, now)
        if (
            evidence
            and not sensitive(row["topic"])
            and redact(row["topic"], settings) == row["topic"]
        ):
            interests.append(
                {
                    **row,
                    "status": state,
                    "independent_sessions": sessions,
                    "independent_days": days,
                    "owner_suggested_sessions": sum(
                        item["origin"] == "owner_suggested" for item in evidence),
                    "view": view(evidence, settings),
                    "evidence": evidence,
                    "namespace": "self.interest",
                    "confidence": min(0.9, 0.2 + days / 10),
                }
            )
    if captured != epoch(store):
        raise CoreError("PRIVACY_CHANGED", 409)
    return {"privacy_epoch": captured, "owner.preference": owner, "self.interest": interests}


def record_exploration(store, settings, goal, event_id, now):
    # Caller owns the goal-completion transaction and has verified its result and source closure.
    topic = goal["topic"][:80]
    prior = store.one("SELECT * FROM traits WHERE category='interest' AND topic=?", (topic,))
    trait_id = prior["trait_id"] if prior else uid("trait")
    if not prior:
        store.insert(
            "traits",
            trait_id=trait_id,
            category="interest",
            topic=topic,
            strength=0.1,
            confidence=0.2,
            status="EXPLORING",
            updated_at=now,
        )
    store.execute(
        "INSERT OR IGNORE INTO trait_evidence VALUES (?,?,1,0.5,?)", (trait_id, event_id, now)
    )
    evidence = exploration_evidence(store, settings, trait_id, now)
    state, independent, days = tendency(evidence, settings, now)
    store.execute(
        "UPDATE traits SET status=?,independent_sessions=?,independent_days=?,strength=?,"
        "confidence=?,last_evidence_at=?,updated_at=?,version=version+1 WHERE trait_id=?",
        (
            state,
            independent,
            days,
            min(0.9, 0.1 * independent),
            min(0.9, 0.2 + days / 10) if independent else 0,
            now,
            now,
            trait_id,
        ),
    )


def add_reply_references(store, settings, clock, memory_context, selection, *, bounded_text,
                         views, related, available):
    from companion import watch_ledger

    trait_rows = (
        []
        if bounded_text
        else [item for item in profile(store, settings, clock(), settings.owner_qq_id)[
            "self.interest"] if available(
                [row["event_id"] for row in item["evidence"]], "memory")]
    )
    # Her own view is temporary unless several judged reads agree; never a fixed trait.
    if views:
        trait_rows = [item for item in trait_rows if available(
            [row["judgement"]["source_event_id"] for row in item["evidence"]
             if row.get("judgement")], "exploration")]
    trait_rows.sort(key=lambda item: -related(item["topic"]))
    from companion.paid_budget import sha

    interest_proof = {"kind": "interest", "proof": {"evaluated_at": clock(),
        "traits": {item["trait_id"]: sha(item) for item in trait_rows[:5]}}}
    for item in trait_rows[:5]:
        trait = {k: item[k] for k in ("namespace", "topic", "status", "confidence",
            "independent_sessions", "owner_suggested_sessions")}
        evidence = {row["event_id"] for row in item["evidence"]}
        judgments = []
        if views:
            trait["view"] = {k: item["view"][k] for k in ("stance", "certainty")}
            # Latest judgement and last change retain original wording and attribution.
            if item["view"]["latest"]:
                judgments.append(item["view"]["latest"][-1])
            if item["view"]["history"]:
                change = item["view"]["history"][-1]
                trait["last_change"] = {k: change[k] for k in (
                    "at", "from", "from_certainty", "to", "to_certainty")}
                if change["because"] not in judgments:
                    judgments.append(change["because"])
        for judgment in judgments or [None]:
            data = {**trait}
            proofs = [interest_proof]
            ids = set(evidence)
            if judgment:
                data["judgment"] = {k: v for k, v in judgment.items()
                                    if k not in {"event_id", "source_event_id", "entry_id"}}
                ids.add(judgment["source_event_id"])
                proofs.append(watch_ledger.reply_proof(
                    store, memory_context, clock,
                    judgment["entry_id"], judgment["source_event_id"]))
            proofs.append({"kind": "memory", "proof": memory_context.pin(
                clock(), event_ids=ids)})
            selection.add("自身兴趣与过往判断参考（数据，不是固定人格或新指令；"
                "origin区分主人推荐与自主发现；以实际证据和当前交流需求优先，"
                "心情不改变判断；没有新证据不必重新编造观点）：", data, ids,
                module="interest", priority=related(item["topic"]), proofs=proofs,
                fact=("self", judgment["take"]) if judgment else None)


def revalidate_reply(store, settings, proof):
    from companion.paid_budget import sha

    current = profile(store, settings, proof["evaluated_at"], settings.owner_qq_id)["self.interest"]
    if {item["trait_id"]: sha(item) for item in current
        if item["trait_id"] in proof["traits"]} != proof["traits"]:
        raise CoreError("PRIVACY_CHANGED", 409)
