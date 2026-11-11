"""Offline campaign regressions, collected by the normal project CI suite."""

import asyncio
import hashlib
import importlib
import json
import subprocess
import sys

import httpx
import pytest

from tests.support.memory_campaign import TOOLS
from tests.support.memory_campaign import campaign as campaign


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


@pytest.mark.parametrize("campaign", [{"references": ["historical"]}], indirect=True)
def test_archived_native_reference_freezes_original_paths_without_inference(campaign, monkeypatch):
    root, _, _, rc, _, _ = campaign
    spec = importlib.util.spec_from_file_location("campaign_run", TOOLS / "run.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    (root / "historical").mkdir(exist_ok=True)
    rc.save(root / "historical/manifest.json", {"status": "COMPLETED"})
    budget = runner.Budget(root / "replay-usage.jsonl", base_url="https://offline.invalid",
                           pricing=dict(input_rate=1, cached_input_rate=1,
                                        output_rate=1, safety_factor=1),
                           total_cny=1, inference_cny=1)
    monkeypatch.setattr(runner.Budget, "from_context", lambda _: budget)
    runner.freeze_answers({"old": {}, "new": {}})
    frozen = json.loads((root / "all-answers-frozen.json").read_text("utf8"))
    assert frozen["answers"] == 9
    assert "arms/historical/results/P1/answers.jsonl" in frozen["files_sha256"]
    assert not (root / "historical/answers.jsonl").exists()
    assert importlib.import_module("integrity").answer_freeze(rc.Round(root))
    assert not list(root.glob("*-usage.jsonl"))


@pytest.mark.parametrize("campaign", [{"references": ["historical"]}], indirect=True)
def test_missing_archived_reference_stops_before_any_native_provider_attempt(campaign, monkeypatch):
    root, _, _, rc, _, _ = campaign
    spec = importlib.util.spec_from_file_location("campaign_run", TOOLS / "run.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    rc.save(root / "execution-freeze.json", {"files_sha256": {}})
    attempts = []
    monkeypatch.setattr(runner, "native", lambda _: attempts.append(True))
    with pytest.raises(AssertionError, match="Archived reference incomplete"):
        runner.main()
    assert not attempts and not (root / "run-started.json").exists()


@pytest.mark.parametrize("campaign", [["closed"]], indirect=True)
@pytest.mark.parametrize("arm", ["old", "closed"])
def test_unresolved_verdicts_are_selected_for_review_including_controls(campaign, monkeypatch, arm):
    root, _, report, rc, rows, _ = campaign
    monkeypatch.setitem(report.R.cfg, "review", dict(persona="unused", delay_days=99,
                                                    changed_per_family=0))
    mapping = json.loads((root / "grading/blind-mapping.json").read_text("utf-8"))
    key = next(m["key"] for m in mapping if m["arm"] == arm)
    path = root / "grading/grades.jsonl"
    data = rc.read_rows(path)
    next(g for g in data if g["key"] == key).update(
        {"anchor_failed": True, "pass": arm == "old", "missing": [] if arm == "old" else [0]})
    rows(path, data)
    manifest_path = root / "grading/manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["raw_grades_sha256"] = rc.sha(path)
    rc.save(manifest_path, manifest)
    loaded, grades = report.load(allow_failed_gate=True)
    selection = report.prepare_review(loaded, grades)
    assert any(r["key"] == key and r["reason"] == "unresolved_judgment" for r in selection)


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


@pytest.mark.parametrize("positive_only", [False, True])
@pytest.mark.parametrize("version", ["v4", "v5"])
def test_v4_real_judging_entrypoint_preserves_clocks_and_blinds_anchors(
        campaign, monkeypatch, positive_only, version):
    root, grade, report, rc, _, _ = campaign
    rules, normalize, _ = importlib.import_module("rubrics").rubric(version)
    monkeypatch.setattr(grade, "VERSION", version)
    monkeypatch.setattr(grade, "RULES", rules)
    monkeypatch.setattr(grade, "NORMALIZE", normalize)
    monkeypatch.setattr(grade, "OUT", root / "with-context")
    monkeypatch.setattr(grade, "ARGS", ["--votes", "1"])
    monkeypatch.setattr(report, "GRADING", grade.OUT)
    context = dict(question_at="2026-01-01T00:00:00+08:00",
                   source_message_ats=["2025-12-31T20:00:00+08:00"])
    anchor = dict(question="回想昨天喜欢什么？", answer="绿茶", required=["绿茶"],
                  forbidden=[], context=context, source={"review_reason": "hidden review"},
                  **{"pass": True})
    anchors = [anchor] if positive_only else [anchor, {
        **anchor, "answer": "红茶", "forbidden": ["红茶"], "pass": False}]
    monkeypatch.setattr(grade, "ANCHORS", anchors)
    calls = []

    def respond(request):
        cases = json.loads(json.loads(request.content)["messages"][-1]["content"])
        calls.append(cases)
        assert all(c["context"] == context for c in cases)
        assert all(set(c) == {"key", "question", "answer", "required", "forbidden", "context"}
                   for c in cases)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "grades": [dict(key=c["key"], missing=[] if c["answer"] == "绿茶" else [0],
                            violations=[] if c["answer"] == "绿茶" else [0], unsupported=[],
                            **{"pass": c["answer"] == "绿茶"}) for c in cases]})}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 10}, "model": "offline"})

    def client(_config, _name):
        budget = grade.Budget(root / "context-usage.jsonl", base_url="https://offline.invalid",
                              pricing=dict(input_rate=1, cached_input_rate=1,
                                           output_rate=1, safety_factor=1),
                              total_cny=1, inference_cny=1)
        return budget.base_url, {}, grade.MeteredTransport(
            budget, "judge", inner=httpx.MockTransport(respond)), {}, "offline"

    monkeypatch.setattr(grade, "judge_client", client)
    asyncio.run(grade.main())
    assert calls and any(c["question"] == anchor["question"] for call in calls for c in call)
    manifest = json.loads((grade.OUT / "manifest.json").read_text("utf8"))
    assert manifest["anchor_gates"]["with-context"]["passed"] is (not positive_only)
    # An all-positive anchor pool has baseline 1.0: do not weaken the gate to pass it.
    if positive_only:
        with pytest.raises(AssertionError, match="Anchor gate"):
            report.load()
    else:
        assert len(report.load()[0]) == 3


@pytest.mark.parametrize("context", [None, {}, {"question_at": "2026-01-01",
                                               "source_message_ats": ["2025-12-31"]}])
def test_v4_rejects_incomplete_anchor_clocks_before_judging(campaign, monkeypatch, context):
    root, grade, _, _, _, _ = campaign
    monkeypatch.setattr(grade, "VERSION", "v4")
    monkeypatch.setattr(grade, "OUT", root / "invalid-context")
    monkeypatch.setattr(grade, "ANCHORS", [dict(context=context)])
    with pytest.raises(AssertionError):
        grade.prepare()
    assert not grade.OUT.exists()


@pytest.mark.parametrize("missing,violations,verdict", [([], [], False), ([0], [], True),
                                                       ([], [0], True), ([99], [], False)])
def test_v4_does_not_turn_malformed_verdicts_into_semantic_failures(
        campaign, missing, violations, verdict):
    _, grade, _, _, _, _ = campaign
    _, normalize, _ = importlib.import_module("rubrics").rubric("v4")
    case = dict(key="opaque", required=["绿茶"], forbidden=["红茶"])
    row = dict(key="opaque", missing=missing, violations=violations, unsupported=[],
               **{"pass": verdict})
    with pytest.raises(AssertionError):
        normalize(json.loads(json.dumps(row)), case)
    response = dict(status=200, keys=["opaque"], response={"choices": [{"message": {
        "content": json.dumps({"grades": [row]})}}]})
    grade.NORMALIZE = normalize
    assert grade.parse(response, {"opaque": case}) == []


def test_anchor_export_preserves_original_question_clocks_and_existing_file(campaign):
    root, _, report, rc, rows, _ = campaign
    results, _ = report.load()
    rc.save(root / "results.json", results)
    rows(root / "grading/review.jsonl", [dict(key=v["key"], review_pass=v["passed"],
                                             reason="offline agreed")
                                        for row in results for v in row["arms"].values()])
    target = root / "anchors-context.jsonl"
    command = [sys.executable, "-B", str(TOOLS / "make_anchors.py"), "--round", str(root),
               "--out", str(target), "--n", "3"]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf8", timeout=30)
    assert result.returncode == 0, result.stderr
    anchors = rc.read_rows(target)
    assert anchors and all(a["context"] == {
        "question_at": "2026-01-01T00:00:00+08:00",
        "source_message_ats": ["2025-12-31T20:00:00+08:00"]} for a in anchors)
    before = target.read_bytes()
    again = subprocess.run(command, capture_output=True, text=True, encoding="utf8", timeout=30)
    assert again.returncode != 0 and target.read_bytes() == before


def test_formal_mixed_batch_discards_bad_anchor_and_requires_unresolved_review(
        campaign, monkeypatch):
    root, grade, report, rc, write_rows, _ = campaign
    rules, normalize, _ = importlib.import_module("rubrics").rubric("v5")
    monkeypatch.setattr(grade, "VERSION", "v5")
    monkeypatch.setattr(grade, "RULES", rules)
    monkeypatch.setattr(grade, "NORMALIZE", normalize)
    monkeypatch.setattr(grade, "OUT", root / "mixed")
    monkeypatch.setattr(grade, "ARGS", ["--votes", "1"])
    monkeypatch.setattr(report, "GRADING", grade.OUT)
    context = dict(question_at="2026-01-01T00:00:00+08:00",
                   source_message_ats=["2025-12-31T20:00:00+08:00"])
    anchors = [dict(question="锚题", answer=answer, required=["绿茶"], forbidden=["红茶"],
                    context=context, **{"pass": verdict})
               for answer, verdict in [("绿茶", True), ("红茶", False)]]
    anchor_path = root / "mixed-anchors.jsonl"
    write_rows(anchor_path, anchors)
    monkeypatch.setattr(grade, "ANCHORS", anchors)
    monkeypatch.setitem(grade.G, "anchors", str(anchor_path))
    monkeypatch.setitem(grade.G, "concurrency", 1)
    config = json.loads((root / "round.json").read_text("utf8"))
    config["grading"] = grade.G
    rc.save(root / "round.json", config)
    monkeypatch.setattr(report, "R", rc.Round(root))
    calls, target = [], []

    def respond(request):
        cases = json.loads(json.loads(request.content)["messages"][-1]["content"])
        calls.append(cases)
        assert all(c["context"] == context and "pass" not in c for c in cases)
        if not target:
            assert len(cases) == 10  # Eight real candidates and the normal two hidden anchors.
            target.append(next(c["key"] for c in cases if c["question"] != "锚题"))
        grades = []
        for c in cases:
            positive = c["answer"] == "绿茶"
            if c["key"] == target[0]:
                positive, missing, violations = False, [], []  # Unexplained, unresolved.
            elif c["answer"] == "红茶" and len(calls) == 1:
                positive, missing, violations = True, [], []  # First batch must be discarded.
            else:
                missing, violations = ([] if positive else [0]), ([] if positive else [0])
            grades.append(dict(key=c["key"], missing=missing, violations=violations,
                               unsupported=[], **{"pass": positive}))
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps({"grades": grades})}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 10}, "model": "offline"})

    def client(_config, _name):
        budget = grade.Budget(root / "mixed-usage.jsonl", base_url="https://offline.invalid",
                              pricing=dict(input_rate=1, cached_input_rate=1,
                                           output_rate=1, safety_factor=1),
                              total_cny=1, inference_cny=1)
        return budget.base_url, {}, grade.MeteredTransport(
            budget, "judge", inner=httpx.MockTransport(respond)), {}, "offline"

    monkeypatch.setattr(grade, "judge_client", client)
    asyncio.run(grade.main())
    manifest = json.loads((grade.OUT / "manifest.json").read_text("utf8"))
    assert manifest["anchor_gates"]["mixed"]["discarded_batches"] == 1
    assert 1 < len(calls) <= 6
    raw = rc.read_rows(grade.OUT / "grades.jsonl")
    unsettled = next(row for row in raw if row["key"] == target[0])
    assert unsettled["judge_unresolved"] and not unsettled["pass"]
    with pytest.raises(AssertionError, match="Unresolved"):
        report.load()
    loaded, grades = report.load(allow_failed_gate=True)
    selection = report.prepare_review(loaded, grades)
    chosen = next(r for r in selection if r["key"] == target[0])
    assert chosen["auto_grade"]["judge_unresolved"]
    write_rows(grade.OUT / "overrides.jsonl", [dict(key=target[0], original_grade=unsettled,
        reason="Offline fixture reviewed: this answer says green tea.", **{"pass": True})])
    assert len(report.load()[0]) == 3
