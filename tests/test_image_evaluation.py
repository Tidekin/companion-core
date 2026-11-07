"""Prepared visual tasks use native evaluation accounting; no external provider calls."""

import json
from copy import deepcopy

import pytest

from companion import evaluation, paid_budget
from companion.contracts import CoreError
from scripts import evaluate_image_understanding as experiment
from tests.support.evaluation import authorize, response, result, work
from tests.support.image_evaluation import prepared


def test_image_rules_facts_and_corrections_have_clean_shared_inputs(core, tmp_path):
    suite, manifests = prepared(core, tmp_path)
    assert suite["request_count"] == 18 and suite["actual_provider_calls"] == 0
    rows = [row for value in manifests for row in value.plan["requests"]]
    pairs = {}
    for row in rows:
        pairs.setdefault(row["case"], []).append(row)
        wire = json.dumps(row["payload"]["messages"], ensure_ascii=False)
        assert "initial_wrong" not in wire and '"gold"' not in wire
        if row["metadata"]["task"] == "bare":
            assert len(row["payload"]["messages"]) == 2
        if row["metadata"]["input"] == "facts" or row["metadata"]["visibility"] == "expired":
            assert "image_url" not in wire
        else:
            assert "data:image/png;base64," in wire
    assert all(len(pair) == 2 and pair[0]["shared_context_sha256"] == pair[1][
        "shared_context_sha256"] for pair in pairs.values())
    cases = experiment.load_cases()
    assert len(cases) == 24
    assert {case["category"] for case in cases} == {
        "repeated_context", "literal_sequence", "color_is_evidence", "ambiguous", "ordinary_photo"}
    for family in {case["family"] for case in cases}:
        assert len({case["split"] for case in cases if case["family"] == family}) == 1


def test_visual_plan_runs_original_worker_and_ledger_without_delivery(core, tmp_path):
    _, manifests = prepared(core, tmp_path)
    manifest = manifests[0]
    submitted = evaluation.submit(core, authorize(core, manifest))
    wire = []
    while True:
        work(core, lambda request: wire.append(json.loads(request.content)) or response(request))
        receipt = result(core, submitted)
        if receipt["state"] != "QUEUED":
            break
    assert receipt["state"] == "SUCCEEDED", receipt["error_code"]
    assert [paid_budget.sha(payload) for payload in wire] == [
        row["payload_sha256"] for row in manifest.plan["requests"]]
    assert all(row["state"] == "SETTLED" for row in receipt["requests"])
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.store.one("SELECT COUNT(*) n FROM model_admissions")["n"] == len(wire)
    experiment.blind([manifest.model_dump()], receipt["requests"], tmp_path / "blinded")
    scored = json.loads((tmp_path / "blinded/review.json").read_text("utf-8"))
    assert all("strategy" not in row and row["scores"]["faithfulness"] is None for row in scored)


@pytest.mark.parametrize("mutation", ["url", "bytes", "unapproved", "wrong_scope"])
def test_visual_evaluation_refuses_changed_or_unapproved_images_before_any_io(
    core, tmp_path, mutation,
):
    _, manifests = prepared(core, tmp_path)
    value = deepcopy(manifests[0].model_dump())
    row = value["plan"]["requests"][0]
    if mutation == "unapproved":
        core.settings.paid_model = core.settings.paid_model.model_copy(update={"image_input": None})
    elif mutation == "wrong_scope":
        value["plan"]["treatment"] = "participation_v2"
    else:
        part = row["payload"]["messages"][1]["content"][1]
        part["image_url"]["url"] = (
            "https://example.com/image.png" if mutation == "url" else "data:image/png;base64,AAAA")
    value["plan"]["plan_sha256"] = paid_budget.sha({
        key: item for key, item in value["plan"].items() if key != "plan_sha256"})
    with pytest.raises(CoreError):
        evaluation.preview(core.store, core.settings, core.clock,
                           evaluation.Manifest.model_validate(value))
    assert core.store.rows("SELECT * FROM model_usage") == []
