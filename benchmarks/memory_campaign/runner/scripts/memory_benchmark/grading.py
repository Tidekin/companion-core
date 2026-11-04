"""Close and hash the complete cohort before opening the original grading key."""

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

from scripts.memory_benchmark.budget import Budget, MeteredTransport
from scripts.memory_benchmark.cli import closure, validate_run
from scripts.memory_benchmark.rubric import RULES, normalize_grade
from scripts.memory_benchmark.runtime import context, credential

CTX = context()
BASE = Path(CTX["run_dir"])
OLD = Path(CTX["fixture_root"])
OUT = BASE / "grading"


def read(path):
    return [json.loads(s) for s in path.read_text("utf-8").splitlines()] if path.exists() else []


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")


def append(path, value):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


async def main():
    validate_run(BASE)
    closure(CTX)
    completion = {"answers_sha256": json.loads((BASE / "answers-frozen.json").read_text("utf-8"))}
    OUT.mkdir(exist_ok=True)
    for name, expected in completion["answers_sha256"].items():
        assert hashlib.sha256((BASE / name).read_bytes()).hexdigest() == expected
    save(OUT / "answers-frozen.json", completion["answers_sha256"])
    questions = {
        q["id"]: q
        for q in read(OLD / "suite-v1/questions.jsonl")
        if q["id"] in set(CTX["selected_ids"])
    }
    answers = sum((read(p) for p in sorted((BASE / "results").glob("*/answers.jsonl"))), [])
    assert (
        len(answers)
        == len({a["id"] for a in answers})
        == len(questions)
        == len(CTX["selected_ids"])
    )
    assert {a["id"] for a in answers} == set(questions)
    # Deliberately opened only after answers are complete and frozen.
    criteria = {c["id"]: c for c in read(OLD / "suite-v1/review-only/criteria.jsonl")}
    rules, normalize = RULES, normalize_grade
    candidates = []
    unanswered = []
    for a in answers:
        if a["status"] != "SUCCEEDED" or not a.get("answer", "").strip():
            unanswered.append({"id": a["id"], "status": a["status"], "error": a.get("error")})
            continue
        candidates.append(
            {
                "key": "new:" + a["id"],
                "id": a["id"],
                "question": questions[a["id"]]["question"],
                "category": questions[a["id"]]["category"],
                "answer": a["answer"],
                "required": criteria[a["id"]]["required"],
                "forbidden": criteria[a["id"]]["forbidden"],
            }
        )
    save(OUT / "unanswered.json", unanswered)
    manifest = {
        "status": "RUNNING",
        "candidates": len(candidates),
        "total_denominator": len(questions),
        "model": CTX["provider"]["model"],
        "endpoint": CTX["provider"]["base_url"],
        "thinking": "disabled",
        "rules_sha256": hashlib.sha256(rules.encode()).hexdigest(),
        "not_independent_human_judging": True,
        "whole_campaign_cap_cny": CTX["budget"]["total_cny"],
        "at": datetime.now(UTC).isoformat(),
    }
    save(OUT / "manifest.json", manifest)
    lookup = {r["key"]: r for r in candidates}

    def recover(response):
        assert response["status"] == 200, "Preserved failed judge request requires inspection"
        parsed = json.loads(response["response"]["choices"][0]["message"]["content"])["grades"]
        assert len(parsed) == len(response["keys"]) and {r["key"] for r in parsed} == set(
            response["keys"]
        )
        existing = {r["key"] for r in read(OUT / "grades.jsonl")}
        for row in parsed:
            normalized = normalize(row, lookup[row["key"]])
            if row["key"] not in existing:
                append(OUT / "grades.jsonl", normalized)

    # A restart can recover saved responses, but never re-call a completed batch.
    for response in read(OUT / "responses.jsonl"):
        recover(response)
    done = {r["key"] for r in read(OUT / "grades.jsonl")}
    requested = {k for r in read(OUT / "requests.jsonl") for k in r["keys"]}
    assert requested <= done, "Pending judge request preserved; do not duplicate an uncertain call"
    rows = [r for r in candidates if r["key"] not in done]
    async with httpx.AsyncClient(
        timeout=120,
        trust_env=False,
        follow_redirects=False,
        transport=MeteredTransport(Budget.from_context(CTX), "judge"),
    ) as client:

        async def batch(part):
            body = {
                "model": CTX["provider"]["model"],
                "messages": [
                    {"role": "system", "content": rules},
                    {"role": "user", "content": json.dumps(part, ensure_ascii=False)},
                ],
                "thinking": {"type": "disabled"},
                "max_tokens": 1536,
                "response_format": {"type": "json_object"},
                "stream": False,
            }
            append(OUT / "requests.jsonl", {"keys": [r["key"] for r in part], "payload": body})
            result = await client.post(
                CTX["provider"]["base_url"] + "/chat/completions",
                headers={"Authorization": "Bearer " + credential(CTX)},
                json=body,
            )
            response = {
                "keys": [r["key"] for r in part],
                "status": result.status_code,
                "response": result.json(),
            }
            append(OUT / "responses.jsonl", response)
            recover(response)
            print("graded", part[0]["key"], len(part), flush=True)

        for start in range(0, len(rows), 24):
            await asyncio.gather(
                *(batch(rows[i : i + 8]) for i in range(start, min(start + 24, len(rows)), 8))
            )
    grades = read(OUT / "grades.jsonl")
    assert len(grades) == len({r["key"] for r in grades}) == len(candidates)
    assert not any(r.get("index_annotation_errors") for r in grades), (
        "Review grade annotation errors"
    )
    manifest.update(status="COMPLETED", finished_at=datetime.now(UTC).isoformat())
    save(OUT / "manifest.json", manifest)


if __name__ == "__main__":
    asyncio.run(main())
