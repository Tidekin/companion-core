"""Strict API-event demultiplexing preserves the original chat/cost report."""

import json
import sys

import pytest

from companion import telemetry
from scripts import benchmark_qq_beta as benchmark


def api_record(**changes):
    return {"schema_version": 1, "evidence_type": "OFFLINE_FIXTURE", "session": "a" * 32,
            "stage": "api_request", "route": "/internal/v1/deliveries/{message_id}/begin-send",
            "method": "POST", "status": 200, "elapsed_ms": 999999,
            "error_class": None, "response_complete": True, **changes}


def stage(stage_name, **changes):
    return {"schema_version": 1, "evidence_type": "OFFLINE_FIXTURE", "stage": stage_name,
            **changes}


def test_mixed_api_records_preserve_chat_latency_budget_and_unknown_report():
    spans = [stage("t_model_request"), stage("t_model_request"),
             stage("t_model_done", elapsed_ms=20, budget_micro_usd=4, model_usage_state="SETTLED"),
             stage("t_model_failed", elapsed_ms=30, budget_micro_usd=6,
                   model_usage_state="UNKNOWN"),
             stage("t_onebot_send_ack", elapsed_ms=50), stage("delivery_uncertain", elapsed_ms=60)]
    states = {"SENT": 1, "DELIVERY_UNCERTAIN": 2}
    original = benchmark.summarize(spans, "OFFLINE_FIXTURE", delivery_states=states)
    mixed = benchmark.summarize(iter([
        api_record(), *spans, api_record(status=None, response_complete=False,
                                        error_class="CancelledError"),
        api_record(evidence_type="LIVE_ONEBOT"),
    ]), "OFFLINE_FIXTURE", delivery_states=states)
    api = mixed.pop("api_requests")
    assert api["validated_records"] == 3 and api["matching_records"] == 2
    assert mixed == original
    assert original["model_budget_estimate_micro_usd"] == 10
    assert original["model_budget_estimate_complete"] is True
    assert original["unknown_model_usage_attempts"] == 1
    assert original["unknown_delivery_count"] == 2
    assert original["observed_uncertain_stage_count"] == 1
    assert original["latency"]["t_onebot_send_ack"]["p95_ms"] == 50
    assert "api_request" not in mixed["stage_counts"]


@pytest.mark.parametrize("field", ["body", "query", "headers", "token", "exception_text",
                                  "trace_id", "budget_micro_usd", "model_usage_state"])
def test_api_unknown_or_cross_schema_fields_are_rejected_without_echo(field):
    record = api_record(**{field: "API_PRIVATE_CANARY"})
    with pytest.raises(ValueError, match="UNRECOGNIZED_API_METRIC_FIELDS") as caught:
        benchmark.summarize([record], "OFFLINE_FIXTURE")
    assert "API_PRIVATE_CANARY" not in str(caught.value)


@pytest.mark.parametrize(("field", "value"), [
    ("schema_version", True), ("schema_version", 2),
    ("session", "API_PRIVATE_CANARY"), ("session", "a" * 33),
    ("evidence_type", "API_PRIVATE_CANARY"),
    ("route", None), ("route", ["API_PRIVATE_CANARY"]),
    ("route", "relative/path"), ("route", "https://private.invalid/path"),
    ("route", "/x?token=API_PRIVATE_CANARY"), ("route", "/x#API_PRIVATE_CANARY"),
    ("route", "/x\nAPI_PRIVATE_CANARY"), ("route", "/x/../private"),
    ("route", "/x//private"), ("route", "/{bad"), ("route", "/" + "a" * 512),
    ("method", "API_PRIVATE_CANARY"), ("method", ["POST"]),
    ("status", True), ("status", "200"), ("status", 99), ("status", 600),
    ("elapsed_ms", True), ("elapsed_ms", -1), ("elapsed_ms", float("nan")),
    ("elapsed_ms", float("inf")), ("elapsed_ms", 10**500),
    ("error_class", "API_PRIVATE_CANARY"), ("error_class", ["HTTPError"]),
    ("response_complete", "yes"),
])
def test_api_schema_rejects_illegal_values_before_evidence_filter(field, value):
    record = api_record(evidence_type="LIVE_ONEBOT", **{field: value}) if field != (
        "evidence_type") else api_record(evidence_type=value)
    with pytest.raises(ValueError, match="INVALID_API_METRIC") as caught:
        benchmark.summarize([record], "OFFLINE_FIXTURE")
    assert "API_PRIVATE_CANARY" not in str(caught.value)


@pytest.mark.parametrize("field", sorted(telemetry.API_FIELDS))
def test_api_schema_requires_every_field(field):
    record = api_record()
    del record[field]
    with pytest.raises(ValueError):
        benchmark.summarize([record], "OFFLINE_FIXTURE")


def test_api_missing_response_headers_requires_incomplete_response():
    with pytest.raises(ValueError, match="INVALID_API_METRIC"):
        benchmark.summarize([api_record(status=None)], "OFFLINE_FIXTURE")


def test_legacy_unknown_fields_are_still_rejected():
    with pytest.raises(ValueError, match="UNRECOGNIZED_METRIC_FIELDS"):
        benchmark.summarize([stage("t_model_done", route="/API_PRIVATE_CANARY")], "OFFLINE_FIXTURE")


def test_transport_body_timings_mix_with_existing_stage_metrics():
    rpc = {"schema_version": 1, "evidence_type": "OFFLINE_FIXTURE", "stage": "onebot_http",
           "monotonic_ns": 123, "elapsed_ms": 18, "error_class": "none"}
    report = benchmark.summarize([rpc, stage("t_reply_group_ack", elapsed_ms=50)],
                                 "OFFLINE_FIXTURE")
    assert report["latency"]["onebot_http"]["p95_ms"] == 18
    assert report["latency"]["t_reply_group_ack"]["p95_ms"] == 50
    with pytest.raises(ValueError, match="UNRECOGNIZED_RPC_METRIC_FIELDS"):
        benchmark.summarize([{**rpc, "body": "PRIVATE_RPC_CANARY"}], "OFFLINE_FIXTURE")
    with pytest.raises(ValueError, match="INVALID_RPC_METRIC"):
        benchmark.summarize([{**rpc, "error_class": "PRIVATE_RPC_CANARY"}], "OFFLINE_FIXTURE")


def test_count_only_output_never_echoes_route_and_cli_never_constructs_app(tmp_path, monkeypatch):
    private = api_record(route="/API_PRIVATE_CANARY", method="OTHER", status=405,
                         error_class="HTTPError")
    rows = [private, stage("t_onebot_send_ack", elapsed_ms=2)]
    source, output = tmp_path / "mixed.jsonl", tmp_path / "summary.json"
    source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "companion.api", None)
    monkeypatch.setattr(sys, "argv", ["benchmark_qq_beta", str(source),
                       "--evidence-type", "OFFLINE_FIXTURE", "--output", str(output)])
    benchmark.main()
    report = json.loads(output.read_text(encoding="utf-8"))
    markdown = output.with_suffix(".md").read_text(encoding="utf-8")
    assert report["api_requests"]["matching_records"] == 1
    assert report["latency"] == {"t_onebot_send_ack": {
        "samples": 1, "p50_ms": 2, "p95_ms": 2, "max_ms": 2}}
    assert "API_PRIVATE_CANARY" not in output.read_text(encoding="utf-8") + markdown
    assert "matching evidence: 1" in markdown
    assert "not message or model spans" in markdown
    # Route aggregation continues to require the separate caller's trusted registry.
    assert telemetry.summarize_api([private], "OFFLINE_FIXTURE", set())["routes"] == []


def test_actual_api_emitter_schema_is_consumed_without_changing_legacy_report(monkeypatch):
    rows = []
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    monkeypatch.setattr(telemetry, "print", lambda line, **kwargs: rows.append(json.loads(line)),
                        raising=False)
    telemetry.emit_api("/internal/v1/health/live", "GET", status=200, elapsed_ms=1,
                       error_class=None, response_complete=True)
    report = benchmark.summarize(rows, "OFFLINE_FIXTURE")
    assert report["api_requests"]["validated_records"] == 1
    assert report["stage_counts"] == report["latency"] == {}
    assert report["model_budget_estimate_micro_usd"] is None
