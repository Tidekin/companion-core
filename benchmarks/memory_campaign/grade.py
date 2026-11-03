"""Version-blind judging of a frozen round with a versioned rubric.

  python grade.py --round DIR                        rubric from round.json -> grading/
  python grade.py --round DIR --rubric v3 --out grading-v3   regrade into a new directory
  --votes N   independent judge passes (batch order shuffled per pass), majority verdict

Answers must be frozen (run.py). Keys are SHA256(freeze + arm + id), so every rubric
grades the same keys. A sent batch without a saved response is never replayed.
"""

import asyncio
import hashlib
import json
import os
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent / "runner")]
import httpx  # noqa: E402
from filelock import FileLock  # noqa: E402
from scripts.memory_benchmark.budget import Budget, MeteredTransport  # noqa: E402
from scripts.memory_benchmark.cli import validate_run  # noqa: E402
from scripts.memory_benchmark.runtime import credential  # noqa: E402

from roundcfg import ARMS, Round, read_rows, save, sha  # noqa: E402
from rubrics import rubric  # noqa: E402

R = Round()
ROOT = R.root
ARGS = sys.argv[1:]
VERSION = ARGS[ARGS.index("--rubric") + 1] if "--rubric" in ARGS else R.cfg["grading"]["rubric"]
OUT = ROOT / (ARGS[ARGS.index("--out") + 1] if "--out" in ARGS else "grading")
RULES, NORMALIZE, _ = rubric(VERSION)
G = R.cfg["grading"]


def append(path, value):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def prepare():
    OUT.mkdir(exist_ok=True)
    frozen = json.loads((ROOT / "all-answers-frozen.json").read_text("utf-8"))
    for name, digest in frozen["files_sha256"].items():
        assert sha(ROOT / name) == digest, name
    configs = {a: validate_run(R.arm_dir(a)) for a in R.cfg["arms"]}
    questions = {q["id"]: q for q in R.questions}
    seed = sha(ROOT / "all-answers-frozen.json")
    cases, mapping, failed = [], [], []
    for arm in ARMS:
        answers = R.arm_answers(arm)
        assert len(answers) == len({a["id"] for a in answers}) == R.total, arm
        for a in answers:
            key = hashlib.sha256((seed + arm + a["id"]).encode()).hexdigest()[:24]
            mapping.append(dict(key=key, arm=arm, id=a["id"], answer_sha256=hashlib.sha256(
                a.get("answer", "").encode()).hexdigest()))
            if a["status"] != "SUCCEEDED" or not a.get("answer", "").strip():
                failed.append(dict(key=key, missing=[], violations=[], execution_failure=True,
                                   status=a["status"], error=a.get("error"), **{"pass": False}))
            else:
                c = R.criteria[a["id"]]
                cases.append(dict(key=key, question=questions[a["id"]]["question"],
                                  answer=a["answer"], required=c["required"],
                                  forbidden=c.get("forbidden", [])))
    mapping.sort(key=lambda r: r["key"])
    cases.sort(key=lambda r: r["key"])
    if (OUT / "blind-mapping.json").exists():
        assert json.loads((OUT / "blind-mapping.json").read_text("utf-8")) == mapping
    else:
        save(OUT / "blind-mapping.json", mapping)
        save(OUT / "execution-failures.json", failed)
        save(OUT / "grading-freeze.json", dict(
            rubric=VERSION, rules_sha256=hashlib.sha256(RULES.encode()).hexdigest(),
            answers_freeze_sha256=seed, criteria_sha256=sha(R.suite / "review-only/criteria.jsonl"),
            total=R.answers_total, successful_candidates=len(cases), failures=len(failed),
            arm_and_version_labels_sent_to_judge=False, not_independent_human=True,
            at=datetime.now(UTC).isoformat(), **{k: v for k, v in G.items() if k != "rubric"}))
    return next(iter(configs.values())), cases, failed


def parse(response, lookup):
    """Grades for the keys this response settled unambiguously.

    A key the judge omitted, invented, or graded twice with different verdicts stays
    ungraded and is judged again alone (a malformed reply carries no verdict, so this is
    not choosing among verdicts). Identical duplicates collapse to one.
    """
    if response["status"] != 200:
        return []
    try:
        parsed = json.loads(response["response"]["choices"][0]["message"]["content"])["grades"]
    except (KeyError, IndexError, TypeError, ValueError):
        return []
    by_key = {}
    for row in parsed if isinstance(parsed, list) else []:
        key = row.get("key") if isinstance(row, dict) else None
        if key in response["keys"]:
            by_key.setdefault(key, []).append(row)
    out = []
    for key, rows in by_key.items():
        if len({json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows}) != 1:
            continue
        try:
            out.append({**NORMALIZE(rows[0], lookup[key]), "rubric": VERSION})
        except (AssertionError, KeyError, TypeError):
            continue
    return out


async def judge(config, cases, failures, lookup, out, seed):
    """One complete, resumable judging pass into `out`; batch composition varies by seed."""
    out.mkdir(exist_ok=True)

    def recover(response):
        done = {r["key"] for r in read_rows(out / "grades.jsonl")}
        for row in parse(response, lookup):
            if row["key"] not in done:
                append(out / "grades.jsonl", row)

    for response in read_rows(out / "responses.jsonl"):
        recover(response)
    sent = {r["batch_id"] for r in read_rows(out / "requests.jsonl")}
    answered = {r["batch_id"] for r in read_rows(out / "responses.jsonl")}
    assert sent <= answered, "Attempted batch lacks a saved response; inspect, never replay"
    done = {r["key"] for r in read_rows(out / "grades.jsonl")}
    for row in failures:
        if row["key"] not in done:
            append(out / "grades.jsonl", {**row, "rubric": VERSION})
    rows = [r for r in cases if r["key"] not in done]
    random.Random(seed).shuffle(rows)
    budget = Budget.from_context(config)
    budget.total_cny = R.cfg["budget"]["deepseek_total_cny"]
    async with httpx.AsyncClient(timeout=120, trust_env=False, follow_redirects=False,
                                 transport=MeteredTransport(budget, "judge")) as client:
        async def batch(part, tag=""):
            keys = [r["key"] for r in part]
            batch_id = hashlib.sha256((VERSION + str(seed) + tag + "".join(keys)).encode()).hexdigest()
            body = dict(model=config["provider"]["model"], messages=[
                dict(role="system", content=RULES),
                dict(role="user", content=json.dumps(part, ensure_ascii=False))],
                thinking={"type": "disabled"}, max_tokens=G["max_tokens"],
                response_format={"type": "json_object"}, stream=False)
            append(out / "requests.jsonl", dict(batch_id=batch_id, keys=keys, payload=body,
                                                at=datetime.now(UTC).isoformat()))
            started = time.monotonic()
            result = await client.post(config["provider"]["base_url"] + "/chat/completions",
                                       headers={"Authorization": "Bearer " + credential(config)},
                                       json=body)
            try:
                value = result.json()
            except ValueError:
                value = {"unparsed_body": result.text}
            response = dict(batch_id=batch_id, keys=keys, status=result.status_code,
                            response=value, elapsed_seconds=time.monotonic() - started,
                            at=datetime.now(UTC).isoformat())
            append(out / "responses.jsonl", response)
            recover(response)

        size, lanes = G["batch_size"], G["concurrency"]
        for start in range(0, len(rows), size * lanes):
            outcomes = await asyncio.gather(*(batch(rows[i:i + size]) for i in range(
                start, min(start + size * lanes, len(rows)), size)), return_exceptions=True)
            errors = [repr(r) for r in outcomes if isinstance(r, BaseException)]
            if errors:
                raise RuntimeError("Saved sibling responses; no automatic retry: " + str(errors))
            print(out.name, "graded", len(read_rows(out / "grades.jsonl")), "of",
                  R.answers_total, flush=True)
        # Keys a malformed reply left without a verdict: one fresh single-key request each.
        for attempt in range(2):
            done = {r["key"] for r in read_rows(out / "grades.jsonl")}
            left = [r for r in cases if r["key"] not in done]
            if not left:
                break
            for part in left:
                await batch([part], tag=f"single{attempt}")
    done = {r["key"] for r in read_rows(out / "grades.jsonl")}
    for case in cases:
        if case["key"] not in done:
            append(out / "grades.jsonl", {"key": case["key"], "pass": False, "missing": [],
                                          "violations": [], "judge_unresolved": True,
                                          "rubric": VERSION})
    grades = read_rows(out / "grades.jsonl")
    assert len(grades) == len({r["key"] for r in grades}) == R.answers_total
    return {r["key"]: r for r in grades}


def majority(passes):
    """Per key: pass when most votes pass; detail fields come from a majority vote."""
    final = []
    for key in sorted(passes[0]):
        votes = [p[key] for p in passes]
        verdict = sum(v["pass"] for v in votes) * 2 > len(votes)
        chosen = next(v for v in votes if v["pass"] == verdict)
        final.append({**chosen, "pass": verdict, "votes": [v["pass"] for v in votes],
                      "agreement": sum(v["pass"] == verdict for v in votes) / len(votes)})
    return final


async def main():
    with FileLock(str(OUT.parent / f".{OUT.name}.lock"), timeout=0):
        config, cases, failures = prepare()
        lookup = {r["key"]: r for r in cases}
        votes = int(ARGS[ARGS.index("--votes") + 1]) if "--votes" in ARGS else G["votes"]
        if votes == 1:
            await judge(config, cases, failures, lookup, OUT, 0)
        else:
            passes = [await judge(config, cases, failures, lookup, OUT / f"vote-{v}", v)
                      for v in range(1, votes + 1)]
            if (OUT / "grades.jsonl").exists():
                (OUT / "grades.jsonl").unlink()
            for row in majority(passes):
                append(OUT / "grades.jsonl", row)
        grades = read_rows(OUT / "grades.jsonl")
        assert len(grades) == len({r["key"] for r in grades}) == R.answers_total
        save(OUT / "manifest.json", dict(
            status="AUTO_COMPLETE_REVIEW_PENDING", rubric=VERSION, votes=votes,
            split_decisions=sum(r.get("agreement", 1) < 1 for r in grades),
            total=R.answers_total, raw_grades_sha256=sha(OUT / "grades.jsonl"),
            at=datetime.now(UTC).isoformat()))


if __name__ == "__main__":
    asyncio.run(main())
