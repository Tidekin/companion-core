"""Synthetic R1 plans on the native evaluation job: no owner sources, bounded batches."""

import json

import pydantic
import pytest

from companion import evaluation, paid_budget
from companion.contracts import CoreError
from companion.model import model_payload
from scripts import evaluate_r1_conversation as experiment
from tests.support.evaluation import authorize, prepare, response, result, work
from tests.support.evaluation_measurement_v2 import rehash


def synthetic(core, tmp_path, *, cases=2, repetitions=3, version=2):
    # Live paid approval and an evaluation-purpose grant; the native limits still apply.
    prepare(core, tmp_path, daily_calls=40, window_calls=40)
    rows = []
    for case in range(cases):
        for repetition in range(repetitions):
            for strategy in ("baseline", "participation_v1"):
                messages = [{"role": "system", "content": "Synthetic " + strategy},
                            {"role": "user", "content": f"SYNTHETIC case {case}"}]
                payload = model_payload(core.settings, messages, "evaluation")
                row = dict(case=f"case-{case}", repetition=repetition, strategy=strategy,
                           payload=payload, payload_sha256=paid_budget.sha(payload),
                           shared_context_sha256=paid_budget.sha(messages[1:]),
                           planned_max_output_tokens=2048)
                if version == 2:
                    row.update(experiment.request_measurement(core.settings, payload))
                rows.append(row)
    plan = dict(requests=rows, request_count=len(rows), case_count=cases, provider="deepseek",
                model="deepseek-flash", actual_provider_calls=0, actual_cost_micro_usd=0,
                status="PREPARED_NO_MODEL_CALLS", rates_micro_usd_per_million={
                    "input": 300000, "cached_input": 6000, "output": 1200000})
    if version == 2:
        plan.update(schema_version=2, repetitions=repetitions,
                    measurement_profile=experiment.measurement_profile(core.settings),
                    estimated_reservation_micro_usd=sum(
                        row["estimated_reservation_micro_usd"] for row in rows))
    rehash(plan)
    return evaluation.Manifest(kind="synthetic", plan=plan, baseline_sha256="a" * 64,
                               source_document_sha256="b" * 64)


def test_synthetic_plan_runs_in_batches_and_never_repeats_a_request(core, tmp_path):
    manifest = synthetic(core, tmp_path)
    request = authorize(core, manifest)
    assert evaluation.preview(core.store, core.settings, core.clock, manifest)[
        "max_cost_micro_usd"] == 1000000
    submitted = evaluation.submit(core, request)
    wire = []

    def observed(req):
        wire.append(paid_budget.sha(json.loads(req.content)))
        return response(req)

    assert work(core, observed)
    job = core.store.one('SELECT state,attempts FROM jobs WHERE job_id=%s', (submitted["job_id"],))
    assert job == {"state": "QUEUED", "attempts": 0} and len(wire) == evaluation.BATCH
    assert result(core, submitted)["state"] == "QUEUED"
    assert work(core, observed)
    view = result(core, submitted)
    assert view["state"] == "SUCCEEDED", view["error_code"]
    assert wire == [row["payload_sha256"] for row in manifest.plan["requests"]]
    assert all(row["complete_response"] for row in view["requests"])
    assert core.store.rows("SELECT * FROM outbox") == []
    assert not work(core, observed) and len(wire) == 12


def test_missing_result_after_a_batch_stops_instead_of_repaying(core, tmp_path):
    manifest = synthetic(core, tmp_path)
    submitted = evaluation.submit(core, authorize(core, manifest))
    wire = []
    work(core, lambda req: wire.append(1) or response(req))
    core.store.execute("DELETE FROM evaluation_results WHERE ordinal=5")
    work(core, lambda req: wire.append(1) or response(req))
    assert len(wire) == evaluation.BATCH
    assert core.store.one('SELECT state,last_error_code FROM jobs WHERE job_id=%s',
                          (submitted["job_id"],)) == {
        "state": "FAILED", "last_error_code": "EVALUATION_PREVIOUS_ATTEMPT_REVIEW_REQUIRED"}


@pytest.mark.parametrize("change", ["v1", "missing_pair", "too_costly", "too_many_cases"])
def test_synthetic_plan_scope_is_checked_before_consent(core, tmp_path, monkeypatch, change):
    manifest = synthetic(core, tmp_path, version=1 if change == "v1" else 2,
                         cases=3 if change == "too_many_cases" else 2)
    if change == "missing_pair":
        plan = manifest.plan
        plan["requests"].pop()
        plan["request_count"] -= 1
        plan["estimated_reservation_micro_usd"] = sum(
            row["estimated_reservation_micro_usd"] for row in plan["requests"])
        rehash(plan)
    elif change == "too_costly":
        monkeypatch.setitem(evaluation.LIMITS["synthetic"], "cost", 1000)
    elif change == "too_many_cases":
        monkeypatch.setitem(evaluation.LIMITS["synthetic"], "cases", 2)
    with pytest.raises(CoreError, match="EVALUATION_MANIFEST_INVALID"):
        evaluation.preview(core.store, core.settings, core.clock, manifest)


def test_sources_match_the_manifest_kind_and_pilot_digest_is_unchanged(core, tmp_path):
    with pytest.raises(pydantic.ValidationError):
        evaluation.Manifest(kind="synthetic", plan={}, source_event_ids=["evt_x"],
                            baseline_sha256="a" * 64, source_document_sha256="b" * 64)
    with pytest.raises(pydantic.ValidationError):
        evaluation.Manifest(plan={}, baseline_sha256="a" * 64, source_document_sha256="b" * 64)
    pilot = prepare(core, tmp_path)
    assert "kind" not in pilot.model_dump()
