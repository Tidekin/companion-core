"""Create a separate v6.1 scoring bank; original evidence and scores remain immutable.

python repair_v6_bank.py ORIGINAL_BANK NEW_BANK
Excludes unsupported C21 co-reference questions; removes redundant C03 unit echo.
Both answer cohorts must use the resulting identical bank when reporting v6.1 scores.
"""
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path


def read(path):
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def write(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), "utf-8")


def hashes(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def repair(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == source or source in destination.parents or destination.exists():
        raise ValueError("Use a new sibling bank directory")
    original = hashes(source)
    questions = read(source / "suite-v1/questions.jsonl")
    criteria = {row["id"]: row for row in read(source / "suite-v1/review-only/criteria.jsonl")}
    inputs = {row["id"]: row for path in (source / "suite-v1/inputs").glob("*.jsonl")
              for row in read(path)}
    excluded, edits, kept = [], [], []
    for question in questions:
        key = question["id"]
        if question["category_id"] == "C21":
            evidence = [inputs[source_id]["text"] for source_id in question["source_ids"]]
            planned = re.search(r"打算把(.+?)寄给", evidence[0])
            later = re.match(r"(.+?)那封", evidence[1]) if len(evidence) > 1 else None
            if planned and later and not planned[1].startswith(later[1]):
                excluded.append({"id": key, "reason": "UNSUPPORTED_ENTITY_COREFERENCE",
                                 "source_ids": question["source_ids"],
                                 "names": [planned[1], later[1]]})
                continue
        if question["category_id"] == "C03":
            before = criteria[key]
            given = [item for item in before["required"] if item.startswith("单位是")
                     and item[3:] in question["question"]]
            if given:
                after = {**before, "required": [v for v in before["required"] if v not in given]}
                if "exact" in before:
                    after["exact"] = [group for group in before["exact"]
                                      if not all(value in question["question"] for value in group)]
                criteria[key] = after
                edits.append({"id": key, "reason": "UNIT_GIVEN_IN_QUESTION",
                              "before": before, "after": after})
        kept.append(question)
    shutil.copytree(source, destination)
    suite = destination / "suite-v1"
    write(suite / "questions.jsonl", kept)
    write(suite / "review-only/criteria.jsonl", [criteria[row["id"]] for row in kept])
    corrections = {"version": "v6.1-evidence-ambiguities", "source_files_sha256": original,
                   "original_questions": len(questions), "scorable_questions": len(kept),
                   "excluded": excluded, "edits": edits,
                   "comparison_rule": "Apply the same bank to old and new answers; label separately from original v6"}
    (destination / "corrections.json").write_text(
        json.dumps(corrections, ensure_ascii=False, indent=2) + "\n", "utf-8")
    manifest = {"version": "v6.1-evidence-ambiguities", "semantic": len(kept),
                "classification": "EXPOSED_ORIGINAL_V6_SCORING_REPAIR",
                "files_sha256": {name: value for name, value in hashes(suite).items()
                                 if name != "manifest.json"}}
    (suite / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
    bundle = {"semantic": len(kept), "independent_lifecycle_cases": 18,
              "files_sha256": {name: value for name, value in hashes(destination).items()
                               if name != "bundle-manifest.json"}}
    (destination / "bundle-manifest.json").write_text(json.dumps(bundle, indent=2) + "\n", "utf-8")
    assert hashes(source) == original
    return corrections


if __name__ == "__main__":
    result = repair(*sys.argv[1:])
    print(json.dumps({key: result[key] for key in (
        "version", "original_questions", "scorable_questions")}, ensure_ascii=False))
