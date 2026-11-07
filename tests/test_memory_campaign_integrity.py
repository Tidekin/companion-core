"""Offline campaign regressions, collected by the normal project CI suite."""

import asyncio
import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

TOOLS = Path(__file__).resolve().parents[1] / "benchmarks/memory_campaign"


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(TOOLS))
    for name in ("roundcfg", "rubrics", "integrity", "grade", "report"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    if "scripts" in sys.modules:
        monkeypatch.setattr(sys.modules["scripts"], "__path__",
                            list(sys.modules["scripts"].__path__))
    monkeypatch.setenv("MEMORY_ROUND", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["grade.py"])
    rc = importlib.import_module("roundcfg")
    rc.save(tmp_path / "round.json", {"name": "offline", "arms": {"old": {}}})
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
    rows(suite / "inputs/P1.jsonl", [dict(id="P1-S1", text="喜欢绿茶。")])
    rc.save(tmp_path / "bank/bundle-manifest.json", {
        "files_sha256": {p.relative_to(tmp_path / "bank").as_posix(): rc.sha(p)
                         for p in suite.rglob("*") if p.is_file()}})
    rc.save(tmp_path / "corpus-freeze.json", {
        "bundle_manifest_sha256": rc.sha(tmp_path / "bank/bundle-manifest.json"),
        "questions_sha256": rc.sha(suite / "questions.jsonl"),
        "criteria_sha256": rc.sha(suite / "review-only/criteria.jsonl")})
    files = []
    for arm in rc.ARMS:
        path = (tmp_path / "rag/answers.jsonl" if arm == "rag" else
                tmp_path / "arms" / arm / "results/P1/answers.jsonl")
        rows(path, [dict(id=q["id"], answer="绿茶", status="SUCCEEDED", elapsed_seconds=1)
                    for q in questions])
        files.append(path)
    rc.save(tmp_path / "all-answers-frozen.json", {
        "status": "ALL_ANSWERS_CLOSED_BEFORE_GOLD_READ", "answers": 9,
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


@pytest.mark.parametrize("change", ["rules", "criteria", "question", "settings", "votes", "judge"])
def test_resume_rejects_changed_grading_inputs(campaign, monkeypatch, change):
    root, grade, _, _, _, _ = campaign
    if change == "rules":
        monkeypatch.setattr(grade, "RULES", grade.RULES + "changed")
    elif change in {"criteria", "question"}:
        path = root / "bank/suite-v1" / (
            "questions.jsonl" if change == "question" else "review-only/criteria.jsonl")
        path.write_text(path.read_text("utf-8").replace("绿茶", "红茶").replace("茶？", "饮料？"),
                        "utf-8")
    elif change == "settings":
        monkeypatch.setitem(grade.G, "anchor_gate_margin", .99)
    elif change == "votes":
        monkeypatch.setattr(grade, "ARGS", ["--votes", "1"])
    else:
        monkeypatch.setitem(grade.R.cfg["judges"]["deepseek"], "extra", {})
    with pytest.raises(AssertionError, match="[Ff]reeze"):
        grade.prepare()


@pytest.mark.parametrize("missing", ["grading-freeze.json", "blind-mapping.json"])
def test_partial_freeze_is_never_overwritten(campaign, missing):
    root, grade, _, _, _, _ = campaign
    (root / "grading" / missing).unlink()
    with pytest.raises(AssertionError, match="[Ff]reeze"):
        grade.prepare()
    assert not (root / "grading" / missing).exists()


def test_cli_vote_override_is_frozen_and_can_resume(campaign, monkeypatch):
    root, grade, _, _, _, _ = campaign
    monkeypatch.setattr(grade, "OUT", root / "one-vote")
    monkeypatch.setattr(grade, "ARGS", ["--votes", "1"])
    grade.prepare()
    before = (root / "one-vote/grading-freeze.json").read_bytes()
    grade.prepare()
    assert before == (root / "one-vote/grading-freeze.json").read_bytes()
    assert json.loads(before)["effective_votes"] == 1


@pytest.mark.parametrize(
    "change", ["criteria", "question", "rubric", "grades", "gate", "duplicate"])
def test_report_rejects_invalid_grading(campaign, change):
    root, _, report, rc, rows, _ = campaign
    if change in {"criteria", "question"}:
        path = root / "bank/suite-v1" / (
            "questions.jsonl" if change == "question" else "review-only/criteria.jsonl")
        path.write_text(path.read_text("utf-8") + "\n", "utf-8")
    elif change == "rubric":
        path = root / "grading/grading-freeze.json"
        frozen = json.loads(path.read_text("utf-8"))
        frozen["rules_sha256"] = "stale"
        rc.save(path, frozen)
    elif change in {"grades", "duplicate"}:
        path = root / "grading/grades.jsonl"
        data = rc.read_rows(path)
        if change == "grades":
            data[0]["pass"] = False
        else:
            data.append(data[0])
        rows(path, data)
        if change == "duplicate":
            manifest = json.loads((root / "grading/manifest.json").read_text("utf-8"))
            manifest["raw_grades_sha256"] = rc.sha(path)
            rc.save(root / "grading/manifest.json", manifest)
    else:
        path = root / "grading/manifest.json"
        manifest = json.loads(path.read_text("utf-8"))
        manifest["status"] = "ANCHOR_GATE_FAILED"
        rc.save(path, manifest)
    with pytest.raises(AssertionError):
        report.load()


def test_failed_anchor_round_can_prepare_review_but_cannot_publish(campaign):
    root, _, report, rc, _, _ = campaign
    path = root / "grading/manifest.json"
    manifest = json.loads(path.read_text("utf-8"))
    manifest["status"] = "ANCHOR_GATE_FAILED"
    rc.save(path, manifest)
    assert len(report.load(allow_failed_gate=True)[0]) == 3
    result = subprocess.run(
        [sys.executable, "-B", str(TOOLS / "report.py"), "--round", str(root),
         "--prepare-review"], capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (root / "grading/review-selection.json").exists()
    assert not (root / "summary.json").exists()
    assert not (root / "memory-report.html").exists()


def test_report_refuses_unfrozen_answer_file(campaign):
    root, _, report, _, rows, _ = campaign
    rows(root / "arms/new/results/extra/answers.jsonl",
         [dict(id="P1-Q/d00", answer="绿茶", status="FAILED")])
    with pytest.raises(AssertionError, match="inventory"):
        report.load()


@pytest.mark.parametrize("change", ["changed_history", "added_history", "bundle"])
def test_report_rejects_changed_source_evidence(campaign, change):
    root, _, report, _, rows, _ = campaign
    path = root / "bank/suite-v1/inputs/P1.jsonl"
    if change == "added_history":
        path = path.with_name("added.jsonl")
    elif change == "bundle":
        path = root / "bank/bundle-manifest.json"
    if change == "bundle":
        path.write_text(path.read_text("utf-8") + "\n", "utf-8")
    else:
        rows(path, [dict(id="P1-S1", text="被改写的历史。")])
    with pytest.raises(AssertionError, match="Corpus freeze"):
        report.load()


@pytest.mark.parametrize("flag", ["judge_unresolved", "anchor_failed"])
def test_unresolved_judgments_require_an_explicit_review_verdict(campaign, flag):
    root, _, report, rc, rows, _ = campaign
    path = root / "grading/grades.jsonl"
    data = rc.read_rows(path)
    data[0].update({"pass": False, flag: True})
    rows(path, data)
    manifest_path = root / "grading/manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["raw_grades_sha256"] = rc.sha(path)
    rc.save(manifest_path, manifest)
    with pytest.raises(AssertionError, match="Unresolved"):
        report.load()
    assert len(report.load(allow_failed_gate=True)[0]) == 3
    rows(root / "grading/overrides.jsonl", [dict(
        key=data[0]["key"], original_grade=data[0], reason="offline reviewed failure",
        **{"pass": False})])
    assert len(report.load()[0]) == 3


def test_no_answered_instances_have_no_semantic_rate(campaign):
    _, _, report, _, _, _ = campaign
    rows, _ = report.load()
    for row in rows:
        row["arms"]["new"].update(answer="", passed=False)
    score = report.score(rows, "new")
    assert score["rate"] == 0 and score["answered_rate"] is None
    assert score["semantic_failures"] == 0 and score["unanswered"] == 3


def test_answered_cohort_excludes_empty_and_failed_answers(campaign):
    _, _, report, _, _, _ = campaign
    rows, _ = report.load()
    rows[0]["arms"]["new"].update(answer="  ", passed=False)
    rows[1]["arms"]["new"].update(status="FAILED", passed=False)
    rows[2]["arms"]["new"].update(passed=False)
    summary = report.summarize(rows)
    score = summary["overall"]["new"]
    assert score["total"] == 3 and score["rate"] == 0
    assert score["answered"] == 1 and score["semantic_failures"] == 1
    assert score["unanswered"] == 2 and score["empty_answers"] == 1
    assert score["execution_failures"] == 1 and score["answered_rate"] == 0
    assert summary["all_arms_answered"]["old"]["total"] == 1
    assert summary["all_arms_answered_ids"] == [rows[2]["id"]]
    assert summary["sample_counts"] == dict(question_instances=3, personas=1, fact_groups=1)
    assert summary["by_persona"]["P1"]["new"]["unanswered"] == 2


def test_report_cli_uses_frozen_results_without_provider_calls(campaign):
    root, grade, report, _, _, _ = campaign
    grade.prepare()  # Identical resume must retain previous evidence.
    rows, _ = report.load()
    assert len(rows) == 3
    result = subprocess.run(
        [sys.executable, "-B", str(TOOLS / "report.py"), "--round", str(root),
         "--allow-partial-review"], capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads((root / "summary.json").read_text("utf-8"))
    assert summary["overall"]["new"]["rate"] == 1
    assert summary["sample_counts"]["fact_groups"] == 1
    html = (root / "memory-report.html").read_text("utf-8")
    assert "事实组" in html and "未作答" in html and "已作答" in html
    assert not list(root.glob("*-usage.jsonl"))
    mapping = json.loads((root / "grading/blind-mapping.json").read_text("utf-8"))
    assert all(m["answer_sha256"] == hashlib.sha256("绿茶".encode()).hexdigest() for m in mapping)


@pytest.mark.parametrize("votes", [1, 3])
def test_judging_entrypoint_freezes_metered_mock_results_and_resumes(campaign, monkeypatch, votes):
    root, grade, report, rc, _, _ = campaign
    out = root / f"mock-{votes}"
    monkeypatch.setattr(grade, "OUT", out)
    monkeypatch.setattr(grade, "ARGS", ["--votes", str(votes)])
    monkeypatch.setattr(report, "GRADING", out)
    calls = []

    def respond(request):
        assert request.url.host == "offline.invalid"
        payload = json.loads(request.content)
        cases = json.loads(payload["messages"][-1]["content"])
        calls.append(cases)
        grades = [dict(key=c["key"], missing=[], violations=[], **{"pass": True}) for c in cases]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({"grades": grades})}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5}, "model": "offline"})

    def client(_config, _name):
        budget = grade.Budget(root / "mock-usage.jsonl", base_url="https://offline.invalid",
                              pricing=dict(input_rate=1, cached_input_rate=1,
                                           output_rate=1, safety_factor=1),
                              total_cny=1, inference_cny=1)
        transport = grade.MeteredTransport(budget, "judge", inner=httpx.MockTransport(respond))
        return budget.base_url, {}, transport, {}, "offline"

    monkeypatch.setattr(grade, "judge_client", client)
    asyncio.run(grade.main())
    rows, _ = report.load()
    assert len(rows) == 3 and all(r["arms"]["new"]["passed"] for r in rows)
    manifest = json.loads((out / "manifest.json").read_text("utf-8"))
    assert manifest["votes"] == votes
    count = len(calls)
    assert count > 0
    asyncio.run(grade.main())
    assert len(calls) == count  # Saved votes must never trigger more provider requests.
    usage = rc.read_rows(root / "mock-usage.jsonl")
    assert len(usage) == count * 2
    assert sum(r["state"] == "SETTLED" for r in usage) == count
