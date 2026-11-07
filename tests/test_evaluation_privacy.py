import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from companion import evaluation
from companion.api import create_app
from companion.policy import ConsentService
from companion.retrieval import SemanticIndex
from tests.support.evaluation import authorize, prepare, response, work
from tests.test_evaluation_process import process_config


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)


def eraser(core, manifest, tmp_path):
    config = tmp_path / "synthetic.json"
    process_config(core, config)

    def erase():
        subprocess.run([
            sys.executable, "-B", str(Path(__file__).with_name("evaluation_erase_process.py")),
            str(config), core.clock(), json.dumps(manifest.source_event_ids),
        ], check=True, capture_output=True, timeout=20)
    return erase


@pytest.mark.parametrize("erase_stage", ["result_read", "final_source_check"])
def test_results_read_racing_independent_erasure_never_returns_erased_text(
    core, tmp_path, monkeypatch, erase_stage,
):
    manifest = prepare(core, tmp_path)
    core.settings.control_token = SecretStr("synthetic-privacy-control-" * 3)
    submitted = evaluation.submit(core, authorize(core, manifest))
    work(core)
    erase = eraser(core, manifest, tmp_path)
    original_one = core.store.one
    triggered = []

    def read_then_erase(sql, *args, **kwargs):
        row = original_one(sql, *args, **kwargs)
        if (erase_stage == "result_read" and
            "SELECT text,text_redacted,finish_reason,evidence_type " in sql and not triggered):
            triggered.append(True)
            erase()
        return row

    original_sources = evaluation._sources
    source_checks = []

    def sources_then_erase(*args, **kwargs):
        sources = original_sources(*args, **kwargs)
        source_checks.append(True)
        if erase_stage == "final_source_check" and len(source_checks) == 2:
            triggered.append(True)
            erase()
        return sources

    monkeypatch.setattr(evaluation, "_sources", sources_then_erase)
    monkeypatch.setattr(core.store, "one", read_then_erase)
    app = create_app(core.settings)
    app.state.core = core
    client = TestClient(app, raise_server_exceptions=False)
    observed = client.get(
        "/internal/v2/evaluations/" + submitted["evaluation_id"],
        headers={"Authorization": "Bearer " + core.settings.control_token.get_secret_value()},
    )
    exposed = observed.status_code == 200 and any(
        r["text"] for r in observed.json()["requests"]
    )
    evidence = {"http_status": observed.status_code, "erasure_completed": bool(triggered),
                "erased_text_returned": exposed,
                "stored_results": len(core.store.rows("SELECT * FROM evaluation_results")),
                "run_redacted": original_one("SELECT redacted FROM evaluation_runs")["redacted"]}
    assert triggered, evidence
    assert observed.status_code in (409, 410), evidence
    assert not exposed, evidence
    assert evidence["stored_results"] == 0 and evidence["run_redacted"] == 1


def test_late_model_response_after_independent_erasure_keeps_fee_not_text(core, tmp_path):
    manifest = prepare(core, tmp_path)
    evaluation.submit(core, authorize(core, manifest))
    erase = eraser(core, manifest, tmp_path)
    calls = []

    def provider(request):
        calls.append(True)
        erase()
        return response(request)

    work(core, provider)
    assert len(calls) == 1
    assert core.store.rows("SELECT * FROM evaluation_results") == []
    assert core.store.one("SELECT redacted FROM evaluation_runs")["redacted"] == 1
    usage = core.store.one("SELECT state,billed_micro_usd FROM model_usage")
    assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] > 0
    assert core.store.rows("SELECT * FROM outbox") == []


def test_revoked_consent_preserves_completed_results(core, tmp_path):
    manifest = prepare(core, tmp_path)
    request = authorize(core, manifest)
    submitted = evaluation.submit(core, request)
    work(core)
    before = evaluation.results(core.store, core.settings, core.clock, submitted["evaluation_id"])
    ConsentService(core.store, core.settings, core.clock).revoke(request.consent_id)
    after = evaluation.results(core.store, core.settings, core.clock, submitted["evaluation_id"])
    assert after["requests"] == before["requests"]
    assert all(row["text"] and row["state"] == "SETTLED" for row in after["requests"])
