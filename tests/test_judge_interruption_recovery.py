"""Real judge checkpoint recovery with mocked provider transport only."""

import asyncio
import hashlib
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture
def judge(tmp_path, monkeypatch):
    tools = Path(__file__).resolve().parents[1] / "benchmarks/memory_campaign"
    monkeypatch.syspath_prepend(str(tools))
    for name in ("grade", "roundcfg", "integrity", "rubrics"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setenv("MEMORY_ROUND", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["grade.py"])
    (tmp_path / "round.json").write_text(json.dumps({
        "name": "offline", "arms": {"core": {}}, "references": [],
        "grading": {"rubric": "v3", "batch_size": 1, "concurrency": 1},
    }), "utf8")
    suite = tmp_path / "bank/suite-v1"
    suite.mkdir(parents=True)
    (suite / "questions.jsonl").write_text("{}\n{}\n{}\n", "utf8")
    grade = importlib.import_module("grade")
    out = tmp_path / "vote-3"
    out.mkdir()
    grade.append(out / "requests.jsonl", {"batch_id": "lost", "keys": ["lost"]})
    grade.append(out / "requests.jsonl", {"batch_id": "saved", "keys": ["saved"]})
    grade.append(out / "grades.jsonl", {"key": "saved", "pass": True})
    grade.append(out / "responses.jsonl", {
        "batch_id": "saved", "keys": ["saved"], "status": 200,
        "response": {"choices": [{"message": {"content": json.dumps({"grades": [{
            "key": "saved", "pass": True, "missing": [], "violations": [],
            "unsupported": [],
        }]})}}]},
    })
    grade.append(tmp_path / "qwen-usage.jsonl", {
        "id": "original", "state": "UNKNOWN", "charged_or_held_cny": 1,
    })
    calls = []

    def provider(request):
        body = json.loads(request.content)
        cases = json.loads(body["messages"][1]["content"])
        calls.extend(row["key"] for row in cases)
        assert calls == ["fresh"], "Attempted candidates must never reach provider again"
        rows = [{"key": row["key"], "pass": True, "missing": [], "violations": [],
                 "unsupported": []} for row in cases]
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps({"grades": rows})}}]})

    monkeypatch.setattr(grade, "judge_client", lambda *_: (
        "https://synthetic.invalid", {}, httpx.MockTransport(provider), {}, "synthetic"))
    cases = [{"key": key, "question": "茶？", "answer": "绿茶", "required": ["绿茶"],
              "forbidden": []} for key in ("lost", "saved", "fresh")]
    yield grade, out, cases, calls
    for name in ("grade", "roundcfg", "integrity", "rubrics"):
        sys.modules.pop(name, None)


def test_default_resume_refuses_missing_response_before_provider_call(judge):
    grade, out, cases, calls = judge
    with pytest.raises(AssertionError, match="never replay"):
        asyncio.run(grade.judge({}, cases, [], {r["key"]: r for r in cases}, out, 3))
    assert calls == [] and not (out / "recovery.jsonl").exists()


def test_recovery_preserves_unknown_and_sends_only_unattempted_keys(judge, monkeypatch):
    grade, out, cases, calls = judge
    before = (out / "requests.jsonl").read_bytes()
    fee_before = (out.parent / "qwen-usage.jsonl").read_bytes()
    monkeypatch.setattr(grade, "ARGS", ["--close-interrupted"])
    result = asyncio.run(grade.judge({}, cases, [], {r["key"]: r for r in cases}, out, 3))
    assert calls == ["fresh"] and set(result) == {"lost", "saved", "fresh"}
    assert result["lost"]["judge_unresolved"] is True
    assert result["saved"]["pass"] is True
    assert (out / "requests.jsonl").read_bytes().startswith(before)
    assert '"batch_id": "lost"' not in (out / "responses.jsonl").read_text()
    assert (out.parent / "qwen-usage.jsonl").read_bytes() == fee_before
    frozen = (out / "grades.jsonl").read_bytes()
    asyncio.run(grade.judge({}, cases, [], {r["key"]: r for r in cases}, out, 3))
    assert calls == ["fresh"] and (out / "grades.jsonl").read_bytes() == frozen


def test_majority_retains_missing_minority_vote_for_explicit_review(judge):
    grade, _, _, _ = judge
    passes = [{"k": {"key": "k", "pass": True}}, {"k": {"key": "k", "pass": True}},
              {"k": {"key": "k", "pass": False, "judge_unresolved": True,
                     "interrupted_review_required": True}}]
    result = grade.majority(passes)[0]
    assert result["pass"] is True and result["votes"] == [True, True, None]
    assert result["judge_unresolved"] and result["interrupted_review_required"]


def test_recovery_keeps_discarded_anchor_verdict_without_resending(judge, monkeypatch):
    grade, out, cases, calls = judge
    monkeypatch.setattr(grade, "ARGS", ["--close-interrupted"])
    monkeypatch.setattr(grade, "ANCHORS", [{
        "question": "茶？", "answer": "红茶", "required": ["绿茶"], "pass": False,
    }])
    (out / "grades.jsonl").unlink()
    (out / "responses.jsonl").unlink()
    grade.append(out / "responses.jsonl", {
        "batch_id": "saved", "keys": ["saved", "anchor"], "status": 200,
        "anchors": {"anchor": 0}, "response": {"choices": [{"message": {
            "content": json.dumps({"grades": [{"key": k, "pass": True, "missing": [],
                "violations": [], "unsupported": []} for k in ("saved", "anchor")]})}}]},
    })
    # No new anchor is needed for the only never-attempted question in this probe.
    monkeypatch.setitem(grade.G, "anchors_per_batch", 0)
    result = asyncio.run(grade.judge({}, cases, [], {r["key"]: r for r in cases}, out, 3))
    assert calls == ["fresh"]
    assert result["saved"]["anchor_failed"] is True
    assert result["saved"]["interrupted_review_required"] is True


@pytest.mark.parametrize("flag", [
    "judge_unresolved", "anchor_failed", "interrupted_review_required",
])
def test_report_requires_explicit_override_for_each_uncertain_grade(judge, monkeypatch, flag):
    grade, out, _, _ = judge
    monkeypatch.delitem(sys.modules, "report", raising=False)
    report = importlib.import_module("report")
    root = out.parent
    grade.save(root / "all-answers-frozen.json", {"files_sha256": {}})
    grade.save(out / "blind-mapping.json", [{
        "key": "k", "arm": "core", "id": "q",
        "answer_sha256": hashlib.sha256("绿茶".encode()).hexdigest(),
    }])
    (out / "grades.jsonl").write_text("", "utf8")
    grade.append(out / "grades.jsonl", {"key": "k", "pass": False, flag: True})
    monkeypatch.setattr(report, "GRADING", out)
    monkeypatch.setattr(report, "R", SimpleNamespace(
        graded_arms=("core",), answers_total=1, questions=[{"id": "q", "source_ids": []}],
        criteria={"q": {"required": ["绿茶"]}}, suite=root / "bank/suite-v1",
        arm_answers=lambda _: [{"id": "q", "answer": "绿茶", "status": "SUCCEEDED"}],
    ))
    with pytest.raises(AssertionError, match="explicit review overrides"):
        report.load()
    assert len(report.load(allow_unresolved=True)[0]) == 1
    grade.append(out / "overrides.jsonl", {
        "key": "k", "original_grade": {"key": "k", "pass": False, flag: True},
        "pass": True, "reason": "Answer contains the required fact.",
    })
    assert report.load()[0][0]["arms"]["core"]["passed"] is True
    sys.modules.pop("report", None)
