import json

import pytest

from companion import telemetry
from companion.contracts import CoreError, InboundEvent
from scripts.benchmark_qq_beta import summarize
from tests.conftest import run
from tests.support.onebot_events import normalized


def assert_public_metrics(lines):
    allowed = {"schema_version", "evidence_type", "session", "trace_id", "stage",
               "monotonic_ns", "elapsed_ms", "budget_micro_usd", "model_usage_state"}
    assert all(set(row) <= allowed for row in lines)
    readable = [{key: value for key, value in row.items()
                 if isinstance(value, str) and key not in {"session", "trace_id"}}
                for row in lines]
    encoded = json.dumps(readable, ensure_ascii=False)
    assert all(private not in encoded for private in ("你好", "10001", "first_token"))


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
    assert_public_metrics(lines)


def test_numeric_metrics_can_contain_digits_from_the_fixture_identifier(monkeypatch, capsys):
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    telemetry.emit("t_model_done", normalized()["trace_id"], at_ns=1591781000100,
                   elapsed_ms=1.0001, budget_micro_usd=10001, model_usage_state="SETTLED")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 1 and lines[0]["monotonic_ns"] == 1591781000100
    assert_public_metrics(lines)


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
