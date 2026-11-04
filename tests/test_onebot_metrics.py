import json

import pytest

from companion.contracts import CoreError, InboundEvent
from scripts.benchmark_qq_beta import summarize
from tests.conftest import run
from tests.support.onebot_events import normalized


def test_core_emits_real_stage_spans_without_private_content(core, monkeypatch, capsys):
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    event = normalized()
    core.ingest(InboundEvent.model_validate(event), event["event_id"])
    core.clock.advance(2)
    assert run(core.run_once("offline-metrics"))
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {row["stage"] for row in lines} >= {
        "t_job_started",
        "t_context_ready",
        "t_model_request",
        "t_model_done",
        "t_job_done",
    }
    assert all(row["trace_id"] == event["trace_id"] for row in lines)
    assert next(r for r in lines if r["stage"] == "t_job_started")["elapsed_ms"] == 2000
    assert "你好" not in json.dumps(lines)
    assert "10001" not in json.dumps(lines)
    assert "first_token" not in json.dumps(lines)


@pytest.mark.parametrize("error", [CoreError("MODEL_RETRYABLE", 503), RuntimeError()])
def test_failed_model_attempt_budget_is_not_lost(core, monkeypatch, capsys, error):
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")
    event = normalized()
    core.ingest(InboundEvent.model_validate(event), event["event_id"])
    core.model.error = error
    assert run(core.run_once("offline-cost"))
    core.model.error = None
    core.clock.advance(6)
    run(core.run_once("offline-cost"))
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    report = summarize(records, "OFFLINE_FIXTURE")
    expected = core.store.one("SELECT SUM(billed_micro_usd) AS n FROM model_usage")["n"]
    assert report["model_budget_estimate_micro_usd"] == expected
    assert report["unknown_model_usage_attempts"] == 1
    assert report["model_budget_estimate_complete"] is True
    assert report["model_invoice_verified"] is False


def test_crash_with_unsettled_metric_is_reported_as_incomplete():
    report = summarize([
        {"schema_version": 1, "evidence_type": "OFFLINE_FIXTURE", "stage": "t_model_request"}
    ], "OFFLINE_FIXTURE")
    assert report["model_budget_estimate_complete"] is False
    assert report["model_budget_estimate_micro_usd"] is None
