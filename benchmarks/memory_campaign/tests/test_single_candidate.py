"""Selected run coverage and reporting keep failed questions in the denominator."""

import importlib.util
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from test_toolkit import make_round  # noqa: E402

from roundcfg import Round  # noqa: E402


def test_one_candidate_counts_only_requested_answers(tmp_path):
    root, _ = make_round(tmp_path, personas=("P1",))
    (root / "round.json").write_text(json.dumps({
        "name": "single", "arms": {"core": "frozen"}, "references": [], "controls": []}), "utf8")
    run = Round(root)
    assert run.graded_arms == ("core",)
    assert run.answers_total == run.total


def test_single_report_retains_failed_questions_without_pair_statistics(tmp_path, monkeypatch):
    root, _ = make_round(tmp_path, personas=("P1",))
    (root / "round.json").write_text(json.dumps({
        "name": "single", "arms": {"core": "frozen"}, "references": [], "controls": []}), "utf8")
    monkeypatch.setenv("MEMORY_ROUND", str(root))
    monkeypatch.setattr(sys, "argv", ["report.py"])
    spec = importlib.util.spec_from_file_location("single_candidate_report", TOOLS / "report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = [{"persona": "P1", "family": "基本", "category_id": "A01", "delay_days": 0,
             "criteria": {}, "input_modalities": ["text"], "arms": {"core": {
                 "passed": i == 0, "status": "SUCCEEDED" if i == 0 else "FAILED",
                 "answer": "合成答案" if i == 0 else "",
                 "elapsed_seconds": 1 + i, "exact_pass": None}}} for i in range(2)]
    summary = module.summarize(rows)
    assert summary["overall"] == {"core": {
        "passed": 1, "total": 2, "rate": .5, "execution_failures": 1,
        "answered": 1, "unanswered": 1, "semantic_failures": 0,
        "answered_rate": 1, "empty_answers": 0}}
    assert "new_minus_old_points" not in summary and "paired_changes" not in summary
