"""Summarize explicit blinded image reviews; never invent a semantic score."""

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from companion.paid_budget import sha  # noqa: E402
from scripts.evaluate_image_understanding import AXES  # noqa: E402


def applicable(metadata):
    axes = set(AXES)
    if metadata["task"] != "correction":
        axes.remove("concrete_review")
    if metadata["input"] == "facts" or metadata["visibility"] == "expired":
        axes.remove("perception")
    return axes


def summarize(review, mapping, outcomes, *, reviewer, latencies=()):
    """Missing and failed attempts remain in each declared condition's denominator."""
    if not reviewer.strip() or len({row["id"] for row in review}) != len(review):
        raise ValueError("Need an identified reviewer and unique scores")
    actual = {row["request_id"]: row for row in outcomes}
    if (len(actual) != len(outcomes) or set(mapping) != {row["id"] for row in review}
        or {value["request_id"] for value in mapping.values()} != set(actual)):
        raise ValueError("All planned attempts must be reviewed, including failures")
    groups, records = defaultdict(list), []
    for row in review:
        identity = mapping[row["id"]]
        observed = actual[identity["request_id"]]
        if (sha([row["case"], row["metadata"]]) != identity["case_metadata_sha256"]
            or row["text"] != observed["text"]
            or row["complete_response"] != observed["complete_response"]):
            raise ValueError("Scored answer changed")
        axes = applicable(row["metadata"])
        if observed["complete_response"]:
            if (not row.get("reason", "").strip()
                or type(row.get("answer_correct")) is not bool
                or any(type(row["scores"].get(axis)) is not int
                       or row["scores"][axis] not in AXES[axis] for axis in axes)):
                raise ValueError("Semantic review is incomplete")
            if row["metadata"]["task"] == "correction" and type(
                row.get("changed_answer")) is not bool:
                raise ValueError("Correction review needs an explicit change judgment")
            scores, correct = {axis: row["scores"][axis] for axis in axes}, row["answer_correct"]
        else:
            scores, correct = {axis: 0 for axis in axes}, False
        metadata = row["metadata"]
        key = ("persona" if row["case"].startswith("persona:") else "rules",
               identity["strategy"], metadata["split"], metadata["task"], metadata["input"],
               metadata["visibility"], metadata.get("correction", "none"))
        value = {"id": row["id"], "metadata": metadata, "correct": correct, "scores": scores,
                 "changed": bool(row.get("changed_answer")),
                 "complete": observed["complete_response"]}
        groups[key].append(value)
        records.append(value)
    result = []
    fields = ("comparison", "strategy", "split", "task", "input", "visibility", "correction")
    for key, values in sorted(groups.items()):
        count = len(values)
        result.append({**dict(zip(fields, key, strict=True)), "attempts": count,
            "complete_responses": sum(row["complete"] for row in values),
            "correct": sum(row["correct"] for row in values),
            "correct_rate": sum(row["correct"] for row in values) / count,
            "mean_scores": {axis: statistics.mean(row["scores"][axis] for row in values)
                            for axis in values[0]["scores"]},
            **({"wrong_to_right": sum(row["correct"] for row in values)}
               if key[-1] in {"correct", "vague"} else {}),
            **({"right_to_wrong": sum(row["changed"] and not row["correct"] and row["complete"]
                                      for row in values)} if key[-1] == "wrong" else {})})
    settled = sum((row.get("usage") or {}).get("billed_micro_usd", 0)
                  for row in outcomes if row["state"] == "SETTLED")
    liability = sum((row.get("usage") or {}).get("billed_micro_usd", 0)
                    for row in outcomes if row["state"] in {"RESERVED", "UNKNOWN"})
    elapsed = sorted(row["elapsed_ms"] for row in latencies if row.get("status") == 200)
    observed_model = any(row.get("evidence_type") == "MODEL_RESPONSE" for row in outcomes)
    return {"status": ("OBSERVED_MODEL_DIAGNOSTIC" if observed_model
                       else "SYNTHETIC_CONTRACT_CHECK"),
            "reviewer": reviewer, "independent_human_review": False,
            "scored_attempts": len(records),
            "states": dict(Counter(row["state"] for row in outcomes)),
            "settled_cost_micro_usd": settled, "unresolved_liability_micro_usd": liability,
            "cost_basis": "native configured rates and usage, not a provider invoice",
            "latency": {"observed_http_responses": len(elapsed),
                        "median_ms": statistics.median(elapsed) if elapsed else None,
                        "p95_ms": elapsed[min(len(elapsed) - 1, int(len(elapsed) * .95))]
                                  if elapsed else None},
            "review_sha256": sha(review), "groups": result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("review", "mapping", "outcomes", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--latencies", type=Path)
    args = parser.parse_args()
    values = [json.loads(path.read_text("utf-8")) for path in (
        args.review, args.mapping, args.outcomes)]
    timings = ([json.loads(line) for line in args.latencies.read_text("utf-8").splitlines()]
               if args.latencies else [])
    report = summarize(*values, reviewer=args.reviewer, latencies=timings)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "status", "scored_attempts", "settled_cost_micro_usd", "unresolved_liability_micro_usd")}))
