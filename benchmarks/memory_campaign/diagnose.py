"""Offline evidence-chain diagnosis by required fields. Reads frozen artifacts only.

For each question and arm:
  executed        terminal SUCCEEDED answer
  sources_read    every source turn perceived (media read, native turn completed)
  extracted       a memory citing a source existed at question time (Core arms)
  fields_in_prompt  every checkable required item has all its key strings together in one
                  evidence unit (a line, a turn or a record) of the prompts sent for that
                  question (all model calls of the exam). The system rules, the question
                  itself, event IDs and hashes are not evidence; numbers match whole
                  (the 6 in 2026 does not count).
  answered        final grade (with review overrides)
Key strings of a required item are its numbers/times plus 2+ character spans that also
occur in the source material; items without such strings are "unchecked", not assumed.
  python diagnose.py --round DIR [--grading grading]
"""

import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent / "runner")]
from roundcfg import ARMS, Round, read_rows, save  # noqa: E402

R = Round()
ROOT = R.root
ARGS = sys.argv[1:]
GRADING = ROOT / (ARGS[ARGS.index("--grading") + 1] if "--grading" in ARGS else "grading")
STAGES = ("executed", "sources_read", "fields_in_prompt", "answered")
NUMBER = re.compile(r"\d+(?::\d+)?(?:-\d+(?::\d+)?)?")
NOISE = re.compile(r"(?:evt|mem|trace|ref|job)_[0-9A-Za-z_]+|[0-9a-f]{16,}")
UNIT = re.compile(r"\n|\\n|\}\s*,\s*\{")


def units(messages, question):
    """Evidence units of one request: every non-system message split into lines and
    records, without the question text, IDs and hashes."""
    out = []
    for message in messages:
        if message.get("role") == "system":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
        text = str(content or "")
        if not text.startswith("{") and question and question in text and len(text) < 2 * len(
                question) + 200:
            continue  # the current question turn
        for unit in UNIT.split(NOISE.sub(" ", text.replace(question, " ") if question else text)):
            if unit := compact(unit):
                out.append(unit)
    return out


def found(keys, unit):
    return all(re.search(rf"(?<!\d){re.escape(k)}(?!\d)", unit) if NUMBER.fullmatch(k)
               else k in unit for k in keys)


def compact(text):
    return "".join(str(text or "").split())


def key_strings(item, material):
    # Only strings the sources themselves contain: derived values (a computed date) and
    # paraphrases cannot be matched literally and stay unchecked instead of failing.
    keys = {n for n in NUMBER.findall(item) if n in material}
    spans = re.findall(r"[一-鿿《》A-Za-z]{2,}", item)
    for span in spans:
        best = ""
        for i in range(len(span)):
            for j in range(i + 2, len(span) + 1):
                piece = span[i:j]
                if len(piece) > len(best) and piece in material:
                    best = piece
        if len(best) >= 2:
            keys.add(best)
    return keys


def source_material(source):
    parts = [source.get("text"), source.get("observed_text"), (source.get("quoted") or {}).get("text")]
    if source.get("asset") and source["asset"].endswith(".txt"):
        parts.append((R.suite / source["asset"]).read_text("utf-8"))
    return compact(" ".join(p for p in parts if p))


def read_ok(source):
    state = (source.get("native_state") or {}).get("state", "SUCCEEDED")
    return state == "SUCCEEDED" and not source.get("media_error")


def core_prompts(arm):
    prompts, turns, evidence = defaultdict(list), {}, {}
    questions = {q["id"]: q["question"] for q in R.questions}
    for persona_dir in sorted((R.arm_dir(arm) / "results").glob("*")):
        if not persona_dir.is_dir():
            continue
        turns.update({r["id"]: r for r in read_rows(persona_dir / "turns-observed.jsonl")})
        for row in read_rows(persona_dir / "requests.jsonl"):
            if row.get("phase") == "exam" and row.get("case"):
                prompts[row["case"]] += units(row["payload"].get("messages", []),
                                              questions.get(row["case"], ""))
        db = sqlite3.connect(f"file:{persona_dir / 'core.sqlite3'}?mode=ro", uri=True)
        by_time = {at[:19]: event for event, at in db.execute(
            "SELECT event_id,occurred_at FROM events WHERE actor_id='owner'")}
        cited = defaultdict(list)
        for event_id, created in db.execute(
                "SELECT me.event_id,m.created_at FROM memory_evidence me JOIN memories m "
                "USING(memory_id)"):
            cited[event_id].append(created)
        for turn in read_rows(persona_dir / "turns-observed.jsonl"):
            event = turn.get("event_id") or by_time.get(
                datetime.fromisoformat(turn["now"]).astimezone(UTC).isoformat()[:19])
            evidence[turn["id"]] = cited.get(event, [])
    return prompts, turns, evidence


def rag_prompts():
    prompts = defaultdict(list)
    questions = {q["id"]: q["question"] for q in R.questions}
    for row in read_rows(ROOT / "rag/requests.jsonl"):
        if row.get("kind") == "answer":
            prompts[row["label"]] += units(row["payload"].get("messages", []),
                                           questions.get(row["label"], ""))
    return prompts


def main():
    results = {r["id"]: r for r in json.loads((ROOT / "results.json").read_text("utf-8"))}
    observed = R.cfg["rag"]["observed_arm"]
    data = {arm: core_prompts(arm) for arm in R.cfg["arms"]}
    rag_turns = data[observed][1]
    data["rag"] = (rag_prompts(), rag_turns, None)
    rows, summary = [], {}
    for arm in ARMS:
        prompts, turns, evidence = data[arm]
        lost, by_category = Counter(), defaultdict(Counter)
        totals = Counter()
        for q in R.questions:
            sources = [turns[s] for s in q["source_ids"]]
            material = "".join(source_material(s) for s in sources)
            items = [key_strings(item, material) for item in R.criteria[q["id"]]["required"]]
            checkable = [keys for keys in items if keys]
            prompt = prompts.get(q["id"], [])
            present = [any(found(keys, unit) for unit in prompt) for keys in checkable]
            item = {
                "executed": results[q["id"]]["arms"][arm]["status"] == "SUCCEEDED",
                "sources_read": all(read_ok(s) for s in sources),
                "extracted": None if evidence is None else any(
                    created <= q["now"] or created[:19] <= datetime.fromisoformat(
                        q["now"]).astimezone(UTC).isoformat()[:19]
                    for s in q["source_ids"] for created in evidence.get(s, [])),
                "fields_in_prompt": all(present),
                "fields_checked": len(checkable), "fields_present": sum(present),
                "fields_unchecked": len(items) - len(checkable),
                "answered": results[q["id"]]["arms"][arm]["passed"]}
            first = "passed" if item["answered"] else next(
                (stage for stage in STAGES if not item[stage]), "passed")
            lost[first] += 1
            by_category[q["category_id"]][first] += 1
            totals.update({k: item[k] for k in ("fields_checked", "fields_present",
                                                "fields_unchecked")})
            rows.append({"id": q["id"], "arm": arm, "category_id": q["category_id"],
                         "first_loss": first, **item})
        mine = [r for r in rows if r["arm"] == arm]
        complete = [r for r in mine if r["executed"] and r["fields_in_prompt"]]
        summary[arm] = {
            "first_loss": dict(lost),
            "executed_rate": sum(r["executed"] for r in mine) / len(mine),
            "sources_read_rate": sum(r["sources_read"] for r in mine) / len(mine),
            "extraction_at_question_time": None if arm == "rag" else
            sum(bool(r["extracted"]) for r in mine) / len(mine),
            "fields_in_prompt_rate": sum(r["fields_in_prompt"] for r in mine) / len(mine),
            "field_presence": totals["fields_present"] / max(1, totals["fields_checked"]),
            "unchecked_items": totals["fields_unchecked"],
            "pass_rate_given_fields": sum(r["answered"] for r in complete) / len(complete)
            if complete else None,
            "passed_without_detected_fields": sum(r["answered"] and not r["fields_in_prompt"]
                                                  for r in mine),
            "by_category": {k: dict(v) for k, v in sorted(by_category.items())}}
    save(ROOT / "evidence-diagnosis.json", {"summary": summary, "rows": rows})
    fmt = lambda v: "—" if v is None else (f"{v:.1%}" if isinstance(v, float) else str(v))  # noqa: E731
    lines = ["| 环节 | " + " | ".join(ARMS) + " |", "|---|" + "---|" * len(ARMS)]
    for key, label in (("executed_rate", "执行成功"), ("sources_read_rate", "来源都读到"),
                       ("extraction_at_question_time", "提问时已抽取"),
                       ("fields_in_prompt_rate", "所需字段全部进prompt"),
                       ("field_presence", "字段级覆盖"),
                       ("pass_rate_given_fields", "字段齐全时通过"),
                       ("passed_without_detected_fields", "通过但未检测到字段"),
                       ("unchecked_items", "无法检查的判据条数")):
        lines.append(f"| {label} | " + " | ".join(fmt(summary[a][key]) for a in ARMS) + " |")
    lines += ["", "| 首个断点 | " + " | ".join(ARMS) + " |", "|---|" + "---|" * len(ARMS)]
    for stage in (*STAGES, "passed"):
        lines.append(f"| {stage} | " + " | ".join(str(summary[a]["first_loss"].get(stage, 0))
                                                  for a in ARMS) + " |")
    (ROOT / "evidence-diagnosis.md").write_text("\n".join(lines) + "\n", "utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
