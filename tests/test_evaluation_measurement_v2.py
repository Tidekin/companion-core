"""Run after the evaluation v2 reader patch; all provider transport is synthetic."""
import asyncio
import copy
import json

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from companion import budget, evaluation, paid_budget, token_measurement
from companion.contracts import CoreError
from companion.core import Core
from companion.model import ModelResponse
from scripts import evaluate_r1_conversation as experiment
from tests.support.core_http import real_core_http
from tests.support.evaluation import authorize, prepare, response, result, work
from tests.support.evaluation_measurement_v2 import rehash
from tests.support.evaluation_process import processes


def v2(core, tmp_path):
    manifest = prepare(core, tmp_path)
    plan = manifest.plan
    plan.update(schema_version=2, repetitions=3,
                measurement_profile=experiment.measurement_profile(core.settings))
    for row in plan["requests"]:
        row.update(experiment.request_measurement(core.settings, row["payload"]))
    plan["estimated_reservation_micro_usd"] = sum(
        r["estimated_reservation_micro_usd"] for r in plan["requests"])
    rehash(plan)
    return manifest


def test_v2_receipts_equal_native_reservations_and_restart_does_not_repay(core, tmp_path):
    manifest = v2(core, tmp_path)
    submitted = evaluation.submit(core, authorize(core, manifest))
    wire = []
    work(core, lambda req: (wire.append(paid_budget.sha(json.loads(req.content))) or response(req)))
    view = result(core, submitted)
    assert view["state"] == "SUCCEEDED", view["error_code"]
    assert wire == [r["payload_sha256"] for r in manifest.plan["requests"]]
    admissions = core.store.rows(
        'SELECT measurement_json FROM model_admissions ORDER BY '
        'record_sequence'
    )
    assert [json.loads(r["measurement_json"]) for r in admissions] == [
        r["measurement"] for r in manifest.plan["requests"]]
    assert len(view["requests"]) == 6
    assert core.store.rows("SELECT * FROM outbox") == []
    core.store.close()
    with Core(core.settings, core.model, core.clock) as restarted:
        assert not work(restarted)
    assert len(wire) == 6


@pytest.mark.parametrize("change", [
    "receipt", "artifact", "policy", "tools", "media", "context", "version",
])
def test_v2_rehashed_drift_rejected_before_admission(core, tmp_path, change):
    manifest = v2(core, tmp_path)
    row = manifest.plan["requests"][0]
    if change == "receipt":
        row["measurement"]["input_tokens"] += 1
    elif change == "artifact":
        manifest.plan["measurement_profile"]["source_sha256"]["companion.model"] = "0" * 64
    elif change == "policy":
        core.settings.paid_model = core.settings.paid_model.model_copy(
            update={"input_estimate_safety_percent": 150})
    elif change == "tools":
        row["payload"]["tools"] = []
    elif change == "media":
        row["payload"]["messages"][-1]["content"] = [{"type": "text", "text": "media"}]
    elif change == "context":
        for item in manifest.plan["requests"]:
            item["payload"]["messages"][-1]["content"] = "large " * 30000
            item["payload_sha256"] = paid_budget.sha(item["payload"])
            item["shared_context_sha256"] = paid_budget.sha(item["payload"]["messages"][1:])
    else:
        manifest.plan["schema_version"] = 3
    rehash(manifest.plan)
    with pytest.raises(CoreError, match="EVALUATION_MANIFEST_INVALID"):
        evaluation._plan(core.settings, manifest.model_dump())
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_v2_unsupported_native_mapping_never_qualifies_for_admission(core, tmp_path):
    manifest = prepare(core, tmp_path)
    core.settings.token_measurement = token_measurement.TokenMeasurementConfig(
        policy="exact_required", provider="deepseek", endpoint="https://api.deepseek.com",
        model="deepseek-flash", renderer="deepseek-v4.1-flash-1017",
        revision=token_measurement.REVISION)
    core.settings.model_url = "https://unverified.synthetic.invalid"
    manifest.plan.update(schema_version=2, repetitions=3,
        measurement_profile=experiment.measurement_profile(core.settings),
        estimated_reservation_micro_usd=None)
    for row in manifest.plan["requests"]:
        row.update(experiment.request_measurement(core.settings, row["payload"]))
        assert row["measurement"]["count_kind"] == "unsupported"
    rehash(manifest.plan)
    with pytest.raises(CoreError, match="EVALUATION_MANIFEST_INVALID"):
        evaluation._plan(core.settings, manifest.model_dump())


def test_v2_unknown_after_restart_keeps_six_denominator(core, tmp_path, monkeypatch):
    manifest = v2(core, tmp_path)
    submitted = evaluation.submit(core, authorize(core, manifest))
    def timeout(request):
        raise httpx.ReadTimeout("synthetic", request=request)
    work(core, timeout)
    core.store.close()
    with Core(core.settings, core.model, core.clock) as restarted:
        assert not work(restarted, timeout)
        rows = result(restarted, submitted)["requests"]
    assert [r["state"] for r in rows] == ["UNKNOWN", *(["NOT_STARTED"] * 5)]
    monkeypatch.setattr(experiment, "ROOT", tmp_path)
    ballot = experiment.blind_export(manifest.plan, rows, tmp_path / ".local/ballot",
                                     measurement_settings=core.settings)
    assert len(ballot) == 3 and not any(r["scorable"] for r in ballot)


def test_v2_settled_without_result_after_crash_is_not_invented_or_repaid(core, tmp_path):
    manifest = v2(core, tmp_path)
    submitted = evaluation.submit(core, authorize(core, manifest))
    job = core.claim_job("synthetic-crashed-worker")
    row = manifest.plan["requests"][0]
    rid = evaluation.request_id(submitted["manifest_sha256"], 0)
    budget.reserve(core.store, core.settings, job["job_id"], "evaluation",
                   row["payload"]["messages"], core.clock(), request_id=rid,
                   logical_request_id=rid, clock=core.clock)
    budget.settle(core.store, core.settings, rid, ModelResponse("lost", 30, 2))
    core.clock.advance(core.settings.lease_seconds + 1)
    core.store.close()
    with Core(core.settings, core.model, core.clock) as restarted:
        assert not work(restarted)
        rows = result(restarted, submitted)["requests"]
        assert len(rows) == 6 and rows[0]["state"] == "SETTLED"
        assert rows[0]["text"] == "" and rows[0]["complete_response"] is False
        assert restarted.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == 1


def test_legacy_v1_plan_is_read_without_measurement_backfill(core, tmp_path):
    manifest = prepare(core, tmp_path)
    manifest.plan["schema_version"] = 1
    rehash(manifest.plan)
    before = copy.deepcopy(manifest.plan)
    evaluation._plan(core.settings, manifest.model_dump())
    assert manifest.plan == before
    assert all("measurement" not in r for r in manifest.plan["requests"])


@pytest.mark.parametrize("boundary", ["normal", "stop", "restore"])
def test_v2_real_tcp_and_independent_worker_preserve_owner_fences(
    empty_database, core, tmp_path, boundary,
):
    from companion import owner_authority
    from companion.backup import backup, restore

    manifest = v2(core, tmp_path)
    core.settings.control_token = SecretStr("synthetic-evaluation-control-token")

    async def execute():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer synthetic-evaluation-control-token"},
        ) as client:
            preview = await client.post("/internal/v2/evaluations/preview",
                                        json=manifest.model_dump(mode="json"))
            assert preview.status_code == 200, preview.text
            consent = await client.post("/internal/v2/consents", json={
                **preview.json()["consent_scope"], "duration_seconds": 600})
            assert consent.status_code == 200, consent.text
            submitted = await client.post("/internal/v2/evaluations", json={
                "manifest": manifest.model_dump(mode="json"),
                "consent_id": consent.json()["consent_id"]})
            assert submitted.status_code == 202, submitted.text
            if boundary == "stop":
                view = owner_authority.current(core.store, core.settings, core.clock)
                stopped = await client.post("/internal/v1/owner-authority/stop", json={
                    "operation_id": "operation-v2-evaluation-stop",
                    "authority": view.authority.as_dict()})
                assert stopped.status_code == 200, stopped.text
            if boundary == "restore":
                key = Fernet.generate_key()
                archive = tmp_path / "prior.enc"
                restored_state = tmp_path / "restored-state"
                backup(core.settings.database, archive, key)
                restore(archive, empty_database, key, core.settings.erasure_ledger,
                        state_directory=restored_state)
                settings = core.settings.model_copy(update={
                    "database": empty_database, "state_directory": str(restored_state),
                })
                with Core(settings, clock=core.clock) as restored:
                    assert await processes(restored, tmp_path / "v2-restored", count=1) == []
                    assert restored.store.rows("SELECT * FROM model_usage") == []
                    assert restored.store.rows("SELECT * FROM outbox") == []
            else:
                sent = await processes(core, tmp_path / "v2-worker", count=1)
                assert sent == ([] if boundary == "stop" else [
                    row["payload_sha256"] for row in manifest.plan["requests"]])
                if boundary == "normal":
                    observed = await client.get("/internal/v2/evaluations/"
                                                + submitted.json()["evaluation_id"])
                    assert observed.status_code == 200, observed.text
                    assert observed.json()["state"] == "SUCCEEDED"
                    measured = core.store.rows(
                        'SELECT measurement_json FROM model_admissions ORDER '
                        'BY record_sequence'
                    )
                    assert [json.loads(row["measurement_json"]) for row in measured] == [
                        row["measurement"] for row in manifest.plan["requests"]]
            assert core.store.rows("SELECT * FROM outbox") == []
    asyncio.run(execute())
