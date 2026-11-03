"""Offline recall comparison of retrieval settings on a finished round. No provider calls.

  python eval_recall.py --round DIR [--arm new] [--source <Core checkout>] [--min-delay 3]
      [--out recall-eval.json]

Raw-turn recall: a question is recalled when every source turn outside the six recent
turns is among the selected turns (production limit). Settings compared: fused only,
fused + second hop, cross-encoder rerank, rerank + second hop.
Fact recall: memories that existed at question time are searched with and without the
cross-encoder; a hit is a returned memory citing any of the question's source events.
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
from companion.memory_references import replay_many  # noqa: E402
from companion.privacy import ErasureLedger  # noqa: E402
from companion.store import Store  # noqa: E402

TURN_SETTINGS = {"fused": dict(turn_recall_rerank=False, turn_recall_second_hop=0),
                 "fused+hop2": dict(turn_recall_rerank=False, turn_recall_second_hop=2),
                 "rerank": dict(turn_recall_rerank=True, turn_recall_second_hop=0),
                 "rerank+hop2": dict(turn_recall_rerank=True, turn_recall_second_hop=2)}


def settings(work, **extra):
    return Settings(database=str(work / "core.sqlite3"), erasure_ledger=str(work / "erasures.jsonl"),
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
        base = settings(work, reranker_model_dir="auto")
        variants = {name: settings(work, reranker_model_dir="auto", **cfg)
                    for name, cfg in TURN_SETTINGS.items()}
        no_rerank = settings(work, reranker_model_dir="")
        store = Store(base.database)
        memory_on = Memory(store, base, ErasureLedger(base.erasure_ledger))
        memory_off = Memory(store, no_rerank, ErasureLedger(no_rerank.erasure_ledger))
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
            recent = {row["event_id"] for row in cands[-6:]}
            cands = [row for row in cands if row["event_id"] not in recent]
            notes = replay_many(store, [row["event_id"] for row in cands],
                                item_chars=turn_recall.FOCUS_SOURCE_CHARS,
                                turn_chars=turn_recall.FOCUS_SOURCE_CHARS * 2)
            docs = [turn_recall._document(row, notes[row["event_id"]], base.timezone)
                    for row in cands]
            source_events = {by_time[utc19(turns[s]["now"])]["event_id"] for s in q["source_ids"]
                             if utc19(turns[s]["now"]) in by_time}
            wanted = source_events - recent
            result = {"id": q["id"], "category_id": q["category_id"], "delay": q["delay_days"]}
            if wanted and docs:
                ids = [row["event_id"] for row in cands]
                for name, cfg in variants.items():
                    start = time.perf_counter()
                    chosen = {ids[i] for i in turn_recall.select(cfg, q["question"], docs, limit)}
                    timing["turns:" + name].append((time.perf_counter() - start) * 1000)
                    result["turns:" + name] = wanted <= chosen
            allowed = [m["memory_id"] for m in memories if m["created_at"][:19] <= utc19(q["now"])]
            if allowed and source_events:
                for name, mem in (("facts:rerank", memory_on), ("facts:fused", memory_off)):
                    start = time.perf_counter()
                    found = mem.search(q["question"], limit=10, memory_ids=allowed, now=q["now"])
                    timing[name].append((time.perf_counter() - start) * 1000)
                    result[name] = any(cites[row["memory_id"]] & source_events for row in found)
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
