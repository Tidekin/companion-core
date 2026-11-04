"""Offline recall comparison of retrieval settings on a finished round. No provider calls.

  python eval_recall.py --round DIR [--arm new] [--source <Core checkout>] [--min-delay 3]
      [--out recall-eval.json]

Raw-turn recall follows the production path: rank every delivered turn (as prefetch
does), drop the recent turns that history already shows, then pick the final `limit`
(reference). A question is recalled when every source turn outside the recent turns is
picked. Settings compared: fused only, fused + second hop, and "v4-hop2", the round-4
prefetch that cut second-hop turns before filtering.
Recent attachments: for sources inside the recent turns that carry a read attachment,
whether every required key string survives the history replay, cut to its first
characters (prefix) or focused on the question (focus).
Fact recall: memories that existed at question time are searched; a hit needs a
returned memory citing a source event (cites) and, stricter, all checkable required
key strings inside the returned memories citing the sources (fields).
Works on copies of the arm's databases.
"""

import json
import shutil
import statistics
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent)]
from diagnose import found, key_strings, source_material  # noqa: E402
from roundcfg import Round, read_rows  # noqa: E402

R = Round()
ARGS = sys.argv[1:]


def arg(name, default):
    return ARGS[ARGS.index(name) + 1] if name in ARGS else default


ARM = arg("--arm", "new")
MIN_DELAY = int(arg("--min-delay", "3"))
OUT = R.root / arg("--out", "recall-eval.json")
SOURCE = Path(arg("--source", str(R.arm_dir(ARM) / "source")))  # Core code under test
sys.path.insert(0, str(SOURCE))

from companion import turn_recall  # noqa: E402
from companion.config import Settings  # noqa: E402
from companion.dialogue_history import recent_sent_sources  # noqa: E402
from companion.memory import Memory  # noqa: E402
from companion.memory_references import replay, replay_many  # noqa: E402
from companion.privacy import ErasureLedger  # noqa: E402
from companion.store import Store  # noqa: E402

TURN_SETTINGS = {"fused": dict(turn_recall_rerank=False, turn_recall_second_hop=0),
                 "fused+hop2": dict(turn_recall_rerank=False, turn_recall_second_hop=2),
                 "v4-hop2": dict(turn_recall_rerank=False, turn_recall_second_hop=2)}
RECENT = 6


def production(cfg, name, query, docs, recent_index, limit):
    """Indices picked after prefetch ranking, recent-turn filtering and the final pick."""
    first, hop = turn_recall.parts(cfg, query, docs)
    extra = cfg.turn_recall_second_hop
    if name == "v4-hop2":
        # Round 4: prefetch fixed 10 first-hop + 2 hop turns before any filtering.
        head = first[:max(1, turn_recall.CANDIDATES - extra)]
        order = head + [i for i in hop if i not in head][:extra]
        return [i for i in order if i not in recent_index][:limit]
    keep = [i for i in first if i not in recent_index]
    chained = [i for i in hop if i not in recent_index]
    return turn_recall.merge(keep, chained, limit, extra)


def settings(work, **extra):
    return Settings(database=str(work / "core.sqlite3"),
                    erasure_ledger=str(work / "erasures.jsonl"),
                    embedding_cache_dir=R.cfg["embedding_cache_dir"], model_key="offline",
                    bridge_token="x" * 32, owner_qq_id="10001", bot_qq_id="10002",
                    memory_enabled=True, **extra)


def utc19(value):
    return datetime.fromisoformat(value).astimezone(UTC).isoformat()[:19]


def main():
    work_root = R.root / "_recall_eval_db"
    rows, timing = [], defaultdict(list)
    limit = 8
    for persona in R.personas:
        work = work_root / persona
        work.mkdir(parents=True, exist_ok=True)
        for name in ("core.sqlite3", "erasures.jsonl"):
            shutil.copy2(R.arm_dir(ARM) / "results" / persona / name, work / name)
        base = settings(work, reranker_model_dir="")
        variants = {name: settings(work, reranker_model_dir="", **cfg)
                    for name, cfg in TURN_SETTINGS.items()}
        store = Store(base.database)
        memory_off = Memory(store, base, ErasureLedger(base.erasure_ledger))
        events = store.rows("SELECT event_id,conversation_id,occurred_at,rowid FROM events "
                            "WHERE actor_id='owner' AND source='qq.onebot' AND redacted=0")
        by_time = {row["occurred_at"][:19]: row for row in events}
        turns = {r["id"]: r for r in read_rows(R.suite / "inputs" / f"{persona}.jsonl")}
        order = sorted(turns.values(), key=lambda r: r["turn"])
        memories = store.rows("SELECT memory_id,created_at FROM memories")
        cites = defaultdict(set)
        for memory_id, event_id in ((r["memory_id"], r["event_id"]) for r in store.rows(
                "SELECT memory_id,event_id FROM memory_evidence")):
            cites[memory_id].add(event_id)
        for q in [q for q in R.per_persona(persona) if q["delay_days"] >= MIN_DELAY]:
            last = next(e for t in reversed(order[:q["cutoff_turn"]])
                        if (e := by_time.get(utc19(t["now"]))))
            cands = recent_sent_sources(store, last["conversation_id"], None, 600,
                                        cutoff_rowid=last["rowid"])
            cands = [{**row, "quoted_text": turn_recall.quoted(store, row)} for row in cands]
            recent = {row["event_id"] for row in cands[-RECENT:]}
            recent_index = {i for i, row in enumerate(cands) if row["event_id"] in recent}
            notes = replay_many(store, [row["event_id"] for row in cands],
                                item_chars=turn_recall.FOCUS_SOURCE_CHARS,
                                turn_chars=turn_recall.FOCUS_SOURCE_CHARS * 2)
            docs = [turn_recall._document(row, notes[row["event_id"]], base.timezone)
                    for row in cands]
            source_events = {by_time[utc19(turns[s]["now"])]["event_id"] for s in q["source_ids"]
                             if utc19(turns[s]["now"]) in by_time}
            wanted = source_events - recent
            result = {"id": q["id"], "category_id": q["category_id"], "delay": q["delay_days"]}
            material = "".join(source_material(turns[s]) for s in q["source_ids"])
            checkable = [k for k in (key_strings(item, material)
                                     for item in R.criteria[q["id"]]["required"]) if k]
            if wanted and docs:
                ids = [row["event_id"] for row in cands]
                for name, cfg in variants.items():
                    start = time.perf_counter()
                    chosen = {ids[i] for i in production(cfg, name, q["question"], docs,
                                                         recent_index, limit)}
                    timing["turns:" + name].append((time.perf_counter() - start) * 1000)
                    result["turns:" + name] = wanted <= chosen
            shown = [e for e in source_events & recent if notes.get(e)]
            # Only items the attachments themselves contain (not the owner's own words).
            full = "".join("".join(note["text"].split()) for e in shown for note in notes[e])
            in_attachment = [keys for keys in checkable if found(keys, full)]
            if shown and in_attachment:
                for name, query in (("attach:prefix", ""), ("attach:focus", q["question"])):
                    text = "".join(note["text"] for e in shown for note in replay(store, e, query))
                    unit = "".join(text.split())
                    result[name] = all(any(found(keys, line) for line in (
                        "".join(x.split()) for x in text.splitlines())) or found(keys, unit)
                        for keys in in_attachment)
            allowed = [m["memory_id"] for m in memories if m["created_at"][:19] <= utc19(q["now"])]
            if allowed and source_events:
                start = time.perf_counter()
                hits = memory_off.search(q["question"], limit=10, memory_ids=allowed, now=q["now"])
                timing["facts:cites"].append((time.perf_counter() - start) * 1000)
                citing = [row for row in hits if cites[row["memory_id"]] & source_events]
                result["facts:cites"] = bool(citing)
                if checkable:
                    text = "".join("".join(row["content"].split()) for row in citing)
                    result["facts:fields"] = all(found(keys, text) for keys in checkable)
            rows.append(result)
        store.close()
        print("evaluated", persona, flush=True)
    keys = sorted({k for r in rows for k in r if ":" in k})
    summary = {k: {"recall": round(sum(r[k] for r in rows if k in r)
                                   / max(1, sum(k in r for r in rows)), 4),
                   "n": sum(k in r for r in rows),
                   "median_ms": round(statistics.median(timing[k]), 1) if timing[k] else None}
               for k in keys}
    by_category = defaultdict(dict)
    for r in rows:
        for k in keys:
            if k in r:
                cell = by_category[r["category_id"]].setdefault(k, [0, 0])
                cell[0] += r[k]
                cell[1] += 1
    json.dump({"arm": ARM, "min_delay": MIN_DELAY, "summary": summary,
               "by_category": by_category, "rows": rows},
              OUT.open("w", encoding="utf-8"), ensure_ascii=False, indent=1)
    shutil.rmtree(work_root, ignore_errors=True)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
