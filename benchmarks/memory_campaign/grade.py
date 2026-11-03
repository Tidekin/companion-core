"""Version-blind judging of a frozen round with a versioned rubric.

  python grade.py --round DIR                        rubric from round.json -> grading/
  python grade.py --round DIR --rubric v2 --out grading-v2   regrade into a new directory

Answers must be frozen (run.py). Keys are SHA256(freeze + arm + id), so every rubric
grades the same keys. A sent batch without a saved response is never replayed.
"""

import asyncio
import hashlib
import json
import os
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
    assert response["status"] == 200, "Saved judge HTTP failure; never replay it"
    parsed = json.loads(response["response"]["choices"][0]["message"]["content"])["grades"]
    assert len(parsed) == len({r["key"] for r in parsed}) == len(response["keys"])
    assert {r["key"] for r in parsed} == set(response["keys"])
    return [{**NORMALIZE(r, lookup[r["key"]]), "rubric": VERSION} for r in parsed]


async def main():
    with FileLock(str(OUT.parent / f".{OUT.name}.lock"), timeout=0):
        config, cases, failures = prepare()
        lookup = {r["key"]: r for r in cases}

        def recover(response):
            done = {r["key"] for r in read_rows(OUT / "grades.jsonl")}
            for row in parse(response, lookup):
                if row["key"] not in done:
                    append(OUT / "grades.jsonl", row)

        for response in read_rows(OUT / "responses.jsonl"):
            recover(response)
        sent = {r["batch_id"] for r in read_rows(OUT / "requests.jsonl")}
        answered = {r["batch_id"] for r in read_rows(OUT / "responses.jsonl")}
        assert sent <= answered, "Attempted batch lacks a saved response; inspect, never replay"
        done = {r["key"] for r in read_rows(OUT / "grades.jsonl")}
        for row in failures:
            if row["key"] not in done:
                append(OUT / "grades.jsonl", {**row, "rubric": VERSION})
        rows = [r for r in cases if r["key"] not in done]
        budget = Budget.from_context(config)
        budget.total_cny = R.cfg["budget"]["deepseek_total_cny"]
        async with httpx.AsyncClient(timeout=120, trust_env=False, follow_redirects=False,
                                     transport=MeteredTransport(budget, "judge")) as client:
            async def batch(part):
                keys = [r["key"] for r in part]
                batch_id = hashlib.sha256((VERSION + "".join(keys)).encode()).hexdigest()
                body = dict(model=config["provider"]["model"], messages=[
                    dict(role="system", content=RULES),
                    dict(role="user", content=json.dumps(part, ensure_ascii=False))],
                    thinking={"type": "disabled"}, max_tokens=G["max_tokens"],
                    response_format={"type": "json_object"}, stream=False)
                append(OUT / "requests.jsonl", dict(batch_id=batch_id, keys=keys, payload=body,
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
                append(OUT / "responses.jsonl", response)
                recover(response)

            size, lanes = G["batch_size"], G["concurrency"]
            for start in range(0, len(rows), size * lanes):
                outcomes = await asyncio.gather(*(batch(rows[i:i + size]) for i in range(
                    start, min(start + size * lanes, len(rows)), size)), return_exceptions=True)
                errors = [repr(r) for r in outcomes if isinstance(r, BaseException)]
                if errors:
                    raise RuntimeError("Saved sibling responses; no automatic retry: " + str(errors))
                print("graded", len(read_rows(OUT / "grades.jsonl")), "of", R.answers_total,
                      flush=True)
        grades = read_rows(OUT / "grades.jsonl")
        assert len(grades) == len({r["key"] for r in grades}) == R.answers_total
        save(OUT / "manifest.json", dict(status="AUTO_COMPLETE_REVIEW_PENDING", rubric=VERSION,
             total=R.answers_total, raw_grades_sha256=sha(OUT / "grades.jsonl"),
             at=datetime.now(UTC).isoformat()))


if __name__ == "__main__":
    asyncio.run(main())
