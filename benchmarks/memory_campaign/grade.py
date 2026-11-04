"""Version-blind judging of a frozen round with a versioned rubric.

  python grade.py --round DIR                        rubric from round.json -> grading/
  python grade.py --round DIR --rubric v3 --out grading-v3   regrade into a new directory
  --votes N   independent judge passes (batch order shuffled per pass), majority verdict
  grading.judges in round.json names the judge of each vote (e.g. deepseek, deepseek, qwen).

Answers must be frozen (run.py). Keys are SHA256(freeze + arm + id), so every rubric
grades the same keys. A sent batch without a saved response is never replayed.

Anchors (grading.anchors): answers with known verdicts, built from an earlier round's
review by make_anchors.py. Each batch carries a few under random keys; a batch that
misjudges any is discarded and its keys are re-judged in fresh batches. After
grading.anchor_retries rounds a key keeps its last discarded verdict, marked
anchor_failed. Each vote reports anchor accuracy against the majority-class baseline;
a vote below baseline + anchor_gate_margin fails the gate (see manifest.json).
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

from roundcfg import Round, read_rows, save, sha  # noqa: E402
from rubrics import rubric  # noqa: E402

R = Round()
ROOT = R.root
ARGS = sys.argv[1:]
VERSION = ARGS[ARGS.index("--rubric") + 1] if "--rubric" in ARGS else R.cfg["grading"]["rubric"]
OUT = ROOT / (ARGS[ARGS.index("--out") + 1] if "--out" in ARGS else "grading")
RULES, NORMALIZE, _ = rubric(VERSION)
G = R.cfg["grading"]
ANCHORS = read_rows(Path(G["anchors"])) if G["anchors"] else []
assert all(type(a["pass"]) is bool and a["required"] for a in ANCHORS), "malformed anchors"
ANCHOR_FIELDS = ("question", "answer", "required", "forbidden")


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
    for arm in R.graded_arms:
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
            anchors_sha256=sha(Path(G["anchors"])) if G["anchors"] else None,
            at=datetime.now(UTC).isoformat(), **{k: v for k, v in G.items() if k != "rubric"}))
    return next(iter(configs.values())), cases, failed


def anchor_cases(response):
    """{key: (anchor case, expected verdict)} for the anchors a saved batch carried."""
    return {key: (dict(key=key, **{f: ANCHORS[i].get(f, []) for f in ANCHOR_FIELDS}),
                  ANCHORS[i]["pass"]) for key, i in response.get("anchors", {}).items()}


def settle(response, lookup):
    """(case grades, every anchor judged right, per-anchor outcomes) for one response."""
    anchors = anchor_cases(response)
    rows = parse(response, {**lookup, **{k: case for k, (case, _) in anchors.items()}})
    by_key = {r["key"]: r for r in rows}
    outcomes = [by_key[k]["pass"] == expected if k in by_key else None
                for k, (_, expected) in anchors.items()]
    return ([r for r in rows if r["key"] not in anchors], all(o is True for o in outcomes),
            outcomes)


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


def judge_client(config, name):
    """(chat base URL, headers, metered transport, body extras, model) of a configured judge."""
    spec = R.cfg["judges"][name]
    if spec["ledger"] == "deepseek":
        budget = Budget.from_context(config)
        budget.total_cny = R.cfg["budget"]["deepseek_total_cny"]
        key, model = credential(config), config["provider"]["model"]
    else:
        key = Path(R.cfg["rag"]["qwen_key_file"] or Path.home() / "Desktop/千问Key.txt").read_text(
            "utf-8-sig").strip()
        assert key and not any(c.isspace() for c in key)
        budget = Budget(R.qwen_ledger, base_url=spec["base_url"], pricing=spec["pricing"],
                        total_cny=R.cfg["budget"]["qwen_total_cny"],
                        inference_cny=R.cfg["budget"]["qwen_total_cny"], concurrency=4)
        model = spec["model"]
    return (budget.base_url, {"Authorization": "Bearer " + key},
            MeteredTransport(budget, "judge"), spec.get("extra", {}), model)


async def judge(config, cases, failures, lookup, out, seed, name="deepseek"):
    """One complete, resumable judging pass into `out`; batch composition varies by seed."""
    out.mkdir(exist_ok=True)
    pending = {}  # key -> last verdict from a batch that misjudged an anchor

    def recover(response):
        done = {r["key"] for r in read_rows(out / "grades.jsonl")}
        rows, anchors_ok, _ = settle(response, lookup)
        for row in rows:
            if row["key"] in done:
                continue
            if anchors_ok:
                append(out / "grades.jsonl", row)
                done.add(row["key"])
            else:
                pending[row["key"]] = row

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
    base, headers, transport, extra, model = judge_client(config, name)
    async with httpx.AsyncClient(timeout=120, trust_env=False, follow_redirects=False,
                                 transport=transport) as client:
        async def batch(part, tag=""):
            keys = [r["key"] for r in part]
            batch_id = hashlib.sha256((VERSION + str(seed) + tag + "".join(keys)).encode()).hexdigest()
            anchors, shown = {}, list(part)
            if ANCHORS:
                pick = random.Random(batch_id)
                for index in pick.sample(range(len(ANCHORS)),
                                         min(G["anchors_per_batch"], len(ANCHORS))):
                    key = hashlib.sha256((batch_id + str(index)).encode()).hexdigest()[:24]
                    anchors[key] = index
                    shown.insert(pick.randrange(len(shown) + 1), anchor_cases(
                        {"anchors": {key: index}})[key][0])
            body = dict(model=model, messages=[
                dict(role="system", content=RULES),
                dict(role="user", content=json.dumps(shown, ensure_ascii=False))],
                max_tokens=G["max_tokens"], response_format={"type": "json_object"},
                stream=False, **extra)
            append(out / "requests.jsonl", dict(batch_id=batch_id, keys=keys, anchors=anchors,
                                                judge=name, payload=body,
                                                at=datetime.now(UTC).isoformat()))
            started = time.monotonic()
            result = await client.post(base + "/chat/completions", headers=headers, json=body)
            try:
                value = result.json()
            except ValueError:
                value = {"unparsed_body": result.text}
            response = dict(batch_id=batch_id, keys=keys + list(anchors), anchors=anchors,
                            judge=name, status=result.status_code, response=value,
                            elapsed_seconds=time.monotonic() - started,
                            at=datetime.now(UTC).isoformat())
            append(out / "responses.jsonl", response)
            recover(response)

        async def run(parts):
            lanes = G["concurrency"]
            for start in range(0, len(parts), lanes):
                outcomes = await asyncio.gather(*(batch(p, t) for p, t in parts[start:start + lanes]),
                                                return_exceptions=True)
                errors = [repr(r) for r in outcomes if isinstance(r, BaseException)]
                if errors:
                    raise RuntimeError("Saved sibling responses; no automatic retry: " + str(errors))
                print(out.name, "graded", len(read_rows(out / "grades.jsonl")), "of",
                      R.answers_total, flush=True)

        size = G["batch_size"]
        await run([(rows[i:i + size], "") for i in range(0, len(rows), size)])
        # Keys without an accepted verdict (a malformed reply or a misjudged anchor): fresh
        # batches with other anchors, then one single-key request each.
        tags = [f"retry{n}" for n in range(G["anchor_retries"] if ANCHORS else 0)]
        for tag in tags + ["single0", "single1"]:
            done = {r["key"] for r in read_rows(out / "grades.jsonl")}
            left = [r for r in cases if r["key"] not in done]
            if not left:
                break
            random.Random(f"{seed}{tag}").shuffle(left)
            step = 1 if tag.startswith("single") else size
            await run([(left[i:i + step], tag) for i in range(0, len(left), step)])
    done = {r["key"] for r in read_rows(out / "grades.jsonl")}
    for case in cases:
        if case["key"] in done:
            continue
        if case["key"] in pending:
            append(out / "grades.jsonl", {**pending[case["key"]], "anchor_failed": True})
        else:
            append(out / "grades.jsonl", {"key": case["key"], "pass": False, "missing": [],
                                          "violations": [], "judge_unresolved": True,
                                          "rubric": VERSION})
    grades = read_rows(out / "grades.jsonl")
    assert len(grades) == len({r["key"] for r in grades}) == R.answers_total
    return {r["key"]: r for r in grades}


def anchor_gate(out):
    """Anchor accuracy of one vote over all its saved batches, against always answering
    the commoner verdict. None without anchors."""
    responses = [r for r in read_rows(out / "responses.jsonl") if r.get("anchors")]
    outcomes, expected, discarded = [], [], 0
    for response in responses:
        _, anchors_ok, results = settle(response, {})
        outcomes += results
        expected += [verdict for _, verdict in anchor_cases(response).values()]
        discarded += not anchors_ok
    if not outcomes:
        return None
    accuracy = sum(o is True for o in outcomes) / len(outcomes)
    baseline = max(sum(expected), len(expected) - sum(expected)) / len(expected)
    return dict(checked=len(outcomes), accuracy=round(accuracy, 4),
                majority_baseline=round(baseline, 4), discarded_batches=discarded,
                batches=len(responses), passed=accuracy >= baseline + G["anchor_gate_margin"])


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
        votes = int(ARGS[ARGS.index("--votes") + 1]) if "--votes" in ARGS else (
            len(G["judges"]) if G["judges"] else G["votes"])
        judges = list(G["judges"] or ["deepseek"] * votes)
        assert len(judges) == votes and set(judges) <= set(R.cfg["judges"]), judges
        dirs = [OUT] if votes == 1 else [OUT / f"vote-{v}" for v in range(1, votes + 1)]
        if votes == 1:
            await judge(config, cases, failures, lookup, OUT, 0, judges[0])
        else:
            passes = [await judge(config, cases, failures, lookup, out, v, judges[v - 1])
                      for v, out in enumerate(dirs, 1)]
            if (OUT / "grades.jsonl").exists():
                (OUT / "grades.jsonl").unlink()
            for row in majority(passes):
                append(OUT / "grades.jsonl", row)
        grades = read_rows(OUT / "grades.jsonl")
        assert len(grades) == len({r["key"] for r in grades}) == R.answers_total
        gates = {out.name: anchor_gate(out) for out in dirs}
        failed = [name for name, gate in gates.items() if gate and not gate["passed"]]
        save(OUT / "manifest.json", dict(
            status="ANCHOR_GATE_FAILED" if failed else "AUTO_COMPLETE_REVIEW_PENDING",
            rubric=VERSION, votes=votes, judges=judges, anchor_gates=gates,
            anchor_failed_keys=sum(bool(r.get("anchor_failed")) for r in grades),
            split_decisions=sum(r.get("agreement", 1) < 1 for r in grades),
            total=R.answers_total, raw_grades_sha256=sha(OUT / "grades.jsonl"),
            at=datetime.now(UTC).isoformat()))


if __name__ == "__main__":
    asyncio.run(main())
