"""Offline memory-campaign round fixture shared by campaign regression suites."""

import importlib
import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "benchmarks/memory_campaign"


@pytest.fixture
def campaign(tmp_path, monkeypatch, request, campaign_runner):
    monkeypatch.syspath_prepend(str(TOOLS))
    for name in ("roundcfg", "rubrics", "integrity", "grade", "report"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    if "scripts" in sys.modules:
        monkeypatch.setattr(sys.modules["scripts"], "__path__",
                            list(sys.modules["scripts"].__path__))
    monkeypatch.setenv("MEMORY_ROUND", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["grade.py"])
    rc = importlib.import_module("roundcfg")
    selection = getattr(request, "param", [])
    controls = selection.get("controls", []) if isinstance(selection, dict) else selection
    references = selection.get("references", ["rag"]) if isinstance(selection, dict) else ["rag"]
    rc.save(tmp_path / "round.json", {"name": "offline", "arms": {"old": {}, "new": {}},
                                     "controls": controls, "references": references})
    suite = tmp_path / "bank/suite-v1"
    (suite / "review-only").mkdir(parents=True)
    (suite / "inputs").mkdir()
    questions = [dict(id=f"P1-Q/d{d:02}", persona="P1", group_id="Q", question="喜欢什么茶？",
                      source_ids=["P1-S1"], category_id="C", category="偏好", family="基本",
                      delay_days=d, now="2026-01-01T00:00:00+08:00", cutoff_turn=1,
                      input_modalities=["text"]) for d in (0, 3, 15)]

    def rows(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in data), "utf-8")

    rows(suite / "questions.jsonl", questions)
    rows(suite / "review-only/criteria.jsonl",
         [dict(id=q["id"], required=["绿茶"], forbidden=[]) for q in questions])
    rows(suite / "inputs/P1.jsonl", [dict(id="P1-S1", text="喜欢绿茶。",
                                        now="2025-12-31T20:00:00+08:00")])
    rc.save(tmp_path / "bank/bundle-manifest.json", {
        "files_sha256": {p.relative_to(tmp_path / "bank").as_posix(): rc.sha(p)
                         for p in suite.rglob("*") if p.is_file()}})
    rc.save(tmp_path / "corpus-freeze.json", {
        "bundle_manifest_sha256": rc.sha(tmp_path / "bank/bundle-manifest.json"),
        "questions_sha256": rc.sha(suite / "questions.jsonl"),
        "criteria_sha256": rc.sha(suite / "review-only/criteria.jsonl")})
    files = []
    for arm in rc.Round(tmp_path).graded_arms:
        path = (tmp_path / arm / "answers.jsonl" if arm == "rag" or arm in controls else
                tmp_path / "arms" / arm / "results/P1/answers.jsonl")
        rows(path, [dict(id=q["id"], answer="绿茶", status="SUCCEEDED", elapsed_seconds=1)
                    for q in questions])
        files.append(path)
    rc.save(tmp_path / "all-answers-frozen.json", {
        "status": "ALL_ANSWERS_CLOSED_BEFORE_GOLD_READ", "answers": len(questions) * len(files),
        "corpus_freeze_sha256": rc.sha(tmp_path / "corpus-freeze.json"),
        "files_sha256": {p.relative_to(tmp_path).as_posix(): rc.sha(p) for p in files}})
    grade = importlib.import_module("grade")
    monkeypatch.setattr(grade, "validate_run", lambda _: {})
    grade.prepare()
    report = importlib.import_module("report")

    def complete():
        mapping = json.loads((tmp_path / "grading/blind-mapping.json").read_text("utf-8"))
        rows(tmp_path / "grading/grades.jsonl",
             [dict(key=m["key"], **{"pass": True}) for m in mapping])
        rc.save(tmp_path / "grading/manifest.json", {
            "status": "AUTO_COMPLETE_REVIEW_PENDING", "anchor_gates": {},
            "grading_freeze_sha256": rc.sha(tmp_path / "grading/grading-freeze.json"),
            "raw_grades_sha256": rc.sha(tmp_path / "grading/grades.jsonl")})

    complete()
    yield tmp_path, grade, report, rc, rows, complete
    for name in ("roundcfg", "rubrics", "integrity", "grade", "report"):
        sys.modules.pop(name, None)
