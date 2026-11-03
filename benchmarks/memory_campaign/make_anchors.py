"""Build judge anchors from an earlier, fully reviewed round. No provider calls.

  python make_anchors.py --round <reviewed round> --out <anchors.jsonl> [--n 40]

An anchor is a graded answer whose verdict a reviewer settled: every override (the
judge got it wrong once, so these are the hard cases), then reviewed answers on which
the judge votes split, then unanimous ones, balanced between pass and fail and capped
per category. Point a later round's grading.anchors at the file; anchors must come from
a different bank than the round they check, so no anchor repeats a graded answer.
"""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent)]
from roundcfg import Round, read_rows  # noqa: E402

R = Round()
ARGS = sys.argv[1:]
OUT = Path(ARGS[ARGS.index("--out") + 1])
N = int(ARGS[ARGS.index("--n") + 1]) if "--n" in ARGS else 40
PER_CATEGORY = 3


def main():
    grading = R.root / "grading"
    reviewed = {r["key"]: r for r in read_rows(grading / "review.jsonl")}
    overridden = {r["key"] for r in read_rows(grading / "overrides.jsonl")}
    assert reviewed, "the source round has no review"
    pool = []
    for row in json.loads((R.root / "results.json").read_text("utf-8")):
        for arm, v in row["arms"].items():
            if v["key"] not in reviewed or v["status"] != "SUCCEEDED":
                continue
            assert reviewed[v["key"]]["review_pass"] == v["passed"], v["key"]
            split = v["auto_grade"].get("agreement", 1) < 1
            rank = 0 if v["key"] in overridden else 1 if split else 2
            pool.append((rank, row, arm, v))
    pool.sort(key=lambda item: (item[0], item[1]["id"], item[2]))
    chosen, per_category, verdicts = [], Counter(), Counter()
    for rank, row, arm, v in pool:
        if len(chosen) >= N:
            break
        # Keep pass/fail within one of each other once the hard cases are in.
        if rank and verdicts[v["passed"]] > verdicts[not v["passed"]]:
            continue
        if per_category[row["category_id"]] >= PER_CATEGORY:
            continue
        per_category[row["category_id"]] += 1
        verdicts[v["passed"]] += 1
        chosen.append({"question": row["question"], "answer": v["answer"],
                       "required": row["criteria"]["required"],
                       "forbidden": row["criteria"].get("forbidden", []), "pass": v["passed"],
                       "source": {"round": R.name, "id": row["id"], "arm": arm,
                                  "kind": ("override", "split_vote", "unanimous")[rank],
                                  "review_reason": reviewed[v["key"]]["reason"]}})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(json.dumps(a, ensure_ascii=False) + "\n" for a in chosen), "utf-8")
    print(json.dumps({"anchors": len(chosen), "pass": verdicts[True], "fail": verdicts[False],
                      "kinds": dict(Counter(a["source"]["kind"] for a in chosen))},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
