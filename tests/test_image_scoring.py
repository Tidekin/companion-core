"""Observed semantic reviews cannot silently drop attempts or invent scores."""

from copy import deepcopy

import pytest

from companion.paid_budget import sha
from scripts.score_image_understanding import summarize


def reviewed():
    packet = [{"id": "a", "case": "case-a", "metadata": {"task": "correction", "input": "original",
        "visibility": "available", "split": "holdout", "correction": "wrong"},
        "text": "模型回应", "complete_response": True, "answer_correct": False,
        "changed_answer": True, "reason": "SYNTHETIC scorer fixture: incorrect objection accepted.",
        "scores": {key: 0 for key in ("perception", "relations", "faithfulness", "concrete_review",
                                     "evidence_honesty", "naturalness")}}]
    mapping = {"a": {"request_id": "req-a", "strategy": "image_understanding",
                      "case_metadata_sha256": sha([packet[0]["case"], packet[0]["metadata"]])}}
    outcomes = [{"request_id": "req-a", "text": "模型回应", "complete_response": True,
        "state": "SETTLED", "evidence_type": "SYNTHETIC_MODEL_RESPONSE",
        "usage": {"billed_micro_usd": 100}}]
    return packet, mapping, outcomes


def test_wrong_correction_and_failure_remain_in_denominator():
    packet, mapping, outcomes = reviewed()
    failed = deepcopy(packet[0])
    failed.update(id="b", text="", complete_response=False, scores={})
    packet.append(failed)
    mapping["b"] = {"request_id": "req-b", "strategy": "image_understanding",
                     "case_metadata_sha256": sha([failed["case"], failed["metadata"]])}
    outcomes.append({"request_id": "req-b", "text": "", "complete_response": False,
                     "state": "UNKNOWN", "usage": {"billed_micro_usd": 200}})
    report = summarize(packet, mapping, outcomes, reviewer="synthetic test")
    assert report["status"] == "SYNTHETIC_CONTRACT_CHECK"
    assert report["scored_attempts"] == 2
    assert report["groups"][0]["attempts"] == 2
    assert report["groups"][0]["right_to_wrong"] == 1
    assert report["settled_cost_micro_usd"] == 100
    assert report["unresolved_liability_micro_usd"] == 200


@pytest.mark.parametrize("mutation", [
    "missing", "unscored", "changed", "boolean_score", "metadata",
])
def test_incomplete_or_changed_review_is_rejected(mutation):
    packet, mapping, outcomes = reviewed()
    if mutation == "missing":
        packet.clear()
    elif mutation == "unscored":
        packet[0]["scores"]["faithfulness"] = None
    elif mutation == "changed":
        packet[0]["text"] = "另一条回应"
    elif mutation == "boolean_score":
        packet[0]["scores"]["faithfulness"] = True
    else:
        packet[0]["metadata"]["visibility"] = "expired"
    with pytest.raises(ValueError):
        summarize(packet, mapping, outcomes, reviewer="synthetic test")
