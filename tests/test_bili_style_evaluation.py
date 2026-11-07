"""CC-STYLE-04 offline R1 plan: participation_v2 versus v2 plus a reviewed observation."""

import json

import pytest

from companion import evaluation
from companion.contracts import CoreError
from scripts import evaluate_r1_conversation as experiment
from tests.support.bili_style_corpus import NOW
from tests.support.bili_style_observation import approved

MULTITURN = experiment.ROOT / "tests/fixtures/r1_multiturn_cases.json"
CASES = experiment.load_cases(MULTITURN)


def context(case, *args):
    return experiment.measurement_defaults(), [
        {"role": "system", "content": "你是主人的 QQ 伙伴。"},
        *[m for turn in case["inputs"]["history"] for m in (
            {"role": "user", "content": turn["user"]},
            {"role": "assistant", "content": turn["assistant"]})],
        {"role": "user", "content": case["inputs"]["text"]}]


@pytest.fixture
def style(tmp_path, monkeypatch):
    monkeypatch.setattr(experiment, "ROOT", tmp_path)
    monkeypatch.setattr(experiment, "build_context", context)
    cand, ids = approved()
    return {"candidates": [cand], "now": NOW, "live_event_ids": ids}


def prepare(tmp_path, style, cases=CASES, name="plan"):
    return experiment.prepare(cases, tmp_path / ".local" / name, treatment=experiment.STYLE,
                              style=style)


def rehashed(plan, **change):
    content = {k: v for k, v in plan.items() if k != "plan_sha256"} | change
    return content | {"plan_sha256": experiment.sha(content)}


def test_multiturn_cases_are_synthetic_and_have_real_history():
    assert len(CASES) >= 8
    assert all(len(case["inputs"]["history"]) >= 2 for case in CASES)
    assert all(case["metadata"]["rubric"] and "expected_output" not in case for case in CASES)
    expression = experiment.ROOT / "tests/fixtures/r1_expression_cases.json"
    frozen = {c["name"] for path in (experiment.DATASET, expression)
              for c in experiment.load_cases(path)}
    assert not frozen & {c["name"] for c in CASES}


def test_style_plan_compares_v2_with_v2_plus_segment(tmp_path, style):
    plan = prepare(tmp_path, style)
    assert plan["treatment"] == experiment.STYLE and plan["control"] == "participation_v2"
    assert {r["strategy"] for r in plan["requests"]} == {"participation_v2", experiment.STYLE}
    assert plan["actual_provider_calls"] == 0
    assert plan["style_candidates"] == [[style["candidates"][0].candidate_id, 1]]
    # Help, serious and closing cases get no casual guidance; the casual ones do.
    applied = {r["case"] for r in plan["requests"] if r.get("style_receipt", {}).get("candidates")}
    assert not applied & {"mt_help_no_joke", "mt_closing", "mt_emotion_serious"}
    assert "mt_daily_share_detail" in applied
    assert plan["style_cases_applied"] == len(applied)
    experiment.validate_plan(plan)


def test_blind_ballot_has_six_axes_and_verdicts_and_hides_arms(tmp_path, style):
    plan = prepare(tmp_path, style)
    rows = [{"case": r["case"], "repetition": r["repetition"], "strategy": r["strategy"],
             "payload_sha256": r["payload_sha256"], "evidence_type": "SYNTHETIC_MODEL_RESPONSE",
             "text": "reply", "state": "SETTLED"} for r in plan["requests"]]
    ballot = experiment.blind_export(plan, rows, tmp_path / ".local/ballot",
                                      expression_review=True)
    assert list(ballot[0]["ratings"]) == list(experiment.STYLE_AXES)
    assert ballot[0]["overall_choices"] == ["A", "B", "TIE", "BOTH_BAD", "UNSCORABLE"]
    raw = (tmp_path / ".local/ballot/ballot.json").read_text("utf-8")
    assert experiment.STYLE not in raw and "B站评论区观察" not in raw
    mapping = json.loads((tmp_path / ".local/ballot/mapping.private.json").read_text("utf-8"))
    assert {mapping[0]["A"], mapping[0]["B"]} == {"participation_v2", experiment.STYLE}
    assert {m["A"] for m in mapping} == {"participation_v2", experiment.STYLE}
    review = json.loads((tmp_path / ".local/ballot/expression-review.json").read_text("utf-8"))
    assert [r["pair_id"] for r in review] == [r["pair_id"] for r in ballot]
    assert experiment.STYLE not in json.dumps(review)
    assert all(not r["A"]["scorable"] and not r["B"]["scorable"] for r in review)


def test_style_requires_reviewed_candidates_and_others_take_none(tmp_path, style):
    with pytest.raises(ValueError, match="candidates"):
        experiment.prepare(CASES, tmp_path / ".local/a", treatment=experiment.STYLE)
    with pytest.raises(ValueError, match="candidates"):
        experiment.prepare(CASES, tmp_path / ".local/b", treatment="participation_v2",
                           style=style)
    with pytest.raises(ValueError, match="nothing to compare"):
        prepare(tmp_path, {**style, "live_event_ids": set()}, name="erased")
    help_only = [c for c in CASES if c["name"] == "mt_help_no_joke"]
    with pytest.raises(ValueError, match="nothing to compare"):
        prepare(tmp_path, style, help_only, name="help")


def test_tampered_segment_or_control_is_rejected(tmp_path, style):
    plan = prepare(tmp_path, style)
    with pytest.raises(ValueError, match="control"):
        experiment.validate_plan(rehashed(plan, control="baseline"))
    without = {k: v for k, v in plan.items() if k not in {"control", "plan_sha256"}}
    with pytest.raises(ValueError, match="control"):
        experiment.validate_plan(without | {"plan_sha256": experiment.sha(without)})
    changed = json.loads(json.dumps(plan))
    row = next(r for r in changed["requests"] if r.get("style_receipt", {}).get("candidates"))
    row["style_receipt"]["candidates"] = [["0" * 64, 1]]
    with pytest.raises(ValueError, match="reviewed candidates"):
        experiment.validate_plan(rehashed(changed))


def test_native_plan_accepts_the_registered_pair_only(tmp_path, style):
    plan = prepare(tmp_path, style)
    settings = experiment.measurement_defaults()

    def manifest(value):
        return evaluation.Manifest(kind="synthetic", plan=value, baseline_sha256="a" * 64,
                                   source_document_sha256="b" * 64).model_dump(mode="json")

    assert len(evaluation._plan(settings, manifest(plan))) == len(plan["requests"])
    for bad in (rehashed(plan, control="baseline"), rehashed(plan, treatment="participation_v3")):
        with pytest.raises(CoreError, match="EVALUATION_MANIFEST_INVALID"):
            evaluation._plan(settings, manifest(bad))


def test_unblinding_turns_a_b_choices_into_treatment_verdicts():
    row = {"pair_id": "pair_001", "A": "participation_v2", "B": experiment.STYLE}
    assert experiment.verdict(row, "B", experiment.STYLE) == "WIN"
    assert experiment.verdict(row, "A", experiment.STYLE) == "LOSE"
    for same in ("TIE", "BOTH_BAD", "UNSCORABLE"):
        assert experiment.verdict(row, same, experiment.STYLE) == same
    with pytest.raises(ValueError):
        experiment.verdict(row, "WIN", experiment.STYLE)
