"""Version-blind judging of a frozen round with a versioned rubric.

  python grade.py --round DIR                        rubric from round.json -> grading/
  python grade.py --round DIR --rubric v3 --out grading-v3   regrade into a new directory
  --votes N   independent judge passes (batch order shuffled per pass), majority verdict
  grading.judges in round.json names the judge of each vote (e.g. deepseek, deepseek, qwen).

Answers must be frozen (run.py). Keys are SHA256(freeze + arm + id), so every rubric
grades the same keys. A sent batch without a saved response is never replayed.
After inspection, --close-interrupted retains attempted keys for explicit review and
continues only previously unattempted keys, without changing usage ledgers.

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

from integrity import check_grading_freeze, grading_identity, vote_settings  # noqa: E402
from roundcfg import Round, read_rows, save, sha, validate_judging_context  # noqa: E402
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
    if VERSION == "v4":
        for anchor in ANCHORS:
            validate_judging_context(anchor.get("context"))
    OUT.mkdir(exist_ok=True)
    votes = int(ARGS[ARGS.index("--votes") + 1]) if "--votes" in ARGS else None
    identity = grading_identity(R, VERSION, RULES, votes)
    freeze_path = OUT / "grading-freeze.json"
    assert freeze_path.exists() == (OUT / "blind-mapping.json").exists(), \
        "Grading freeze incomplete; preserve this archive and use a fresh --out directory"
    if (OUT / "blind-mapping.json").exists():
        check_grading_freeze(json.loads(freeze_path.read_text("utf-8")), identity)
    else:
        assert not any(OUT.iterdir()), "Grading freeze missing for existing evidence; use a fresh --out directory"
    configs = {a: validate_run(R.arm_dir(a)) for a in R.cfg["arms"]}
    questions = {q["id"]: q for q in R.questions}
    seed = identity["answers_freeze_sha256"]
    cases, mapping, failed = [], [], []
    for arm in R.graded_arms:
        answers = R.arm_answers(arm)
        assert len(answers) == len({a["id"] for a in answers}) == R.total, arm
        assert {a["id"] for a in answers} == set(questions), arm
        for a in answers:
            key = hashlib.sha256((seed + arm + a["id"]).encode()).hexdigest()[:24]
            mapping.append(dict(key=key, arm=arm, id=a["id"], answer_sha256=hashlib.sha256(
                a.get("answer", "").encode()).hexdigest()))
            if a["status"] != "SUCCEEDED" or not a.get("answer", "").strip():
                failed.append(dict(key=key, missing=[], violations=[], execution_failure=True,
                                   status=a["status"], error=a.get("error"), **{"pass": False}))
            else:
                c = R.criteria[a["id"]]
                case = dict(key=key, question=questions[a["id"]]["question"],
                            answer=a["answer"], required=c["required"],
                            forbidden=c.get("forbidden", []))
                if VERSION == "v4":
                    case["context"] = R.judging_context(questions[a["id"]])
                cases.append(case)
    mapping.sort(key=lambda r: r["key"])
    cases.sort(key=lambda r: r["key"])
    if (OUT / "blind-mapping.json").exists():
        assert json.loads((OUT / "blind-mapping.json").read_text("utf-8")) == mapping
    else:
        save(OUT / "blind-mapping.json", mapping)
        save(OUT / "execution-failures.json", failed)
        save(freeze_path, dict(
            **identity, successful_candidates=len(cases), failures=len(failed),
            arm_and_version_labels_sent_to_judge=False, not_independent_human=True,
            at=datetime.now(UTC).isoformat()))
    return next(iter(configs.values())), cases, failed


def anchor_cases(response):
    """{key: (anchor case, expected verdict)} for the anchors a saved batch carried."""
    fields = ANCHOR_FIELDS + (("context",) if VERSION == "v4" else ())
    return {key: (dict(key=key, **{f: ANCHORS[i].get(f, []) for f in fields}),
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
    requests = read_rows(out / "requests.jsonl")
    sent = {r["batch_id"] for r in requests}
    answered = {r["batch_id"] for r in read_rows(out / "responses.jsonl")}
    if "--close-interrupted" in ARGS:
        done = {r["key"] for r in read_rows(out / "grades.jsonl")}
        attempted = {key for request in requests for key in request["keys"]}
        left = sorted(attempted - done)
        if left:
            append(out / "recovery.jsonl", dict(
                at=datetime.now(UTC).isoformat(), method="ATTEMPTED_KEYS_NOT_REPLAYED",
                keys=left, missing_response_batches=sorted(sent - answered),
                requests_sha256=sha(out / "requests.jsonl"),
                responses_sha256=sha(out / "responses.jsonl")
                if (out / "responses.jsonl").exists() else None,
                fees_unchanged=True))
        for key in left:
            assert key in lookup, "Attempted key is outside the frozen candidates"
            if key in pending:
                row = {**pending[key], "anchor_failed": True}
            else:
                row = dict(key=key, **{"pass": False}, missing=[], violations=[],
                           judge_unresolved=True, rubric=VERSION)
            append(out / "grades.jsonl", {**row, "interrupted_review_required": True})
    else:
        assert sent <= answered, "Attempted batch lacks a saved response; inspect, never replay"
    done = {r["key"] for r in read_rows(out / "grades.jsonl")}
    for row in failures:
        if row["key"] not in done:
            append(out / "grades.jsonl", {**row, "rubric": VERSION})
    rows = [r for r in cases if r["key"] not in done]
    if not rows:
        grades = read_rows(out / "grades.jsonl")
        assert len(grades) == len({r["key"] for r in grades}) == R.answers_total
        return {r["key"]: r for r in grades}
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
        # A missing vote is not a semantic failure; publication requires an explicit
        # review even when the other two votes already determine the majority.
        if any(v.get("judge_unresolved") for v in votes):
            final[-1]["judge_unresolved"] = True
            final[-1]["votes"] = [None if v.get("judge_unresolved") else v["pass"] for v in votes]
        for flag in ("anchor_failed", "interrupted_review_required"):
            if any(v.get(flag) for v in votes):
                final[-1][flag] = True
    return final


async def main():
    with FileLock(str(OUT.parent / f".{OUT.name}.lock"), timeout=0):
        config, cases, failures = prepare()
        lookup = {r["key"]: r for r in cases}
        votes, judges = vote_settings(R, int(ARGS[ARGS.index("--votes") + 1]) if "--votes" in ARGS else None)
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
            grading_freeze_sha256=sha(OUT / "grading-freeze.json"),
            at=datetime.now(UTC).isoformat()))


if __name__ == "__main__":
    asyncio.run(main())
