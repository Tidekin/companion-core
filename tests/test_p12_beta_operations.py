import copy
import json
import os
import subprocess
import sys
import tomllib

import pytest

from companion.beta_observation import inspect_context
from companion.config import Settings
from scripts.summarize_beta_feedback import summarize
from scripts.validate_beta_feedback import validate_record
from tests.conftest import ingest, run
from tests.postgres_fixture import logical_state
from tests.support.p12_beta_operations import ROOT, bundle, feedback, inspector, write


def phone_pair(record, seconds):
    record["phone"].update(received=True, sent_at_utc="2026-10-22T01:00:00Z",
                           received_at_utc=f"2026-10-22T01:00:0{seconds}Z",
                           clock_basis="same_device_utc", pair_evidence_sha256="a" * 64,
                           visible_reply_count=1, duplicate_check_evidence_sha256="b" * 64)


def test_real_inspector_read_and_summary_preserve_database_and_unknown_evidence(core, tmp_path):
    ingest(core, text="PRIVATE_OWNER_TEXT", at="2026-10-21T16:01:00+00:00")
    run(core.run_once("worker"))
    core.store.execute("UPDATE outbox SET state='SENT'")
    before = logical_state(core.store)
    model_calls = len(core.model.calls)
    report = inspect_context(core.settings, "trace_test_1")
    assert report["trace"]["event_occurred_at_utc"] == "2026-10-21T16:01:00+00:00"
    assert report["trace"]["jobs"][0]["model_calls"][0]["logical_request_ref_sha256"] is None
    result = summarize(bundle(tmp_path, [(feedback(), report)]))
    group = result["groups"]["local_synthetic"]
    assert group["reported_event_dates"] == ["2026-10-22"]
    assert group["outbox_states"] == {"SENT": 1}
    assert group["phone"]["received"] is None
    assert group["model"]["prompt_tokens"]["sum"] is None
    assert group["model"]["logical_requests"] is None
    assert group["authenticated_inputs"] is group["send_attempts"] is None
    assert group["continuous_healthy_seconds"] is None
    assert result["evidence_authenticated"] is result["runtime_authorized"] is False
    assert "PRIVATE_OWNER_TEXT" not in json.dumps(result)
    assert "PRIVATE_REASON_NEVER_PRINT" not in json.dumps(result)
    assert logical_state(core.store) == before and len(core.model.calls) == model_calls
    core.store.execute("UPDATE events SET redacted=1")
    assert inspect_context(core.settings, "trace_test_1")["trace"]["event_occurred_at_utc"] is None


def test_groups_nulls_unknown_reservations_dates_and_duplicate_invoice_are_separate(tmp_path):
    records = [feedback("authorized_runtime") for _ in range(3)]
    for record, seconds in zip(records[:2], (1, 2), strict=True):
        phone_pair(record, seconds)
        record["provider_invoice"] = {"micro_usd": 11, "evidence_sha256": "f" * 64}
        record["qq_types"] = ["reply"]
        record["coverage_observations"]["reply"] = "ID_ONLY"
    reports = [inspector(1), inspector(2, "2026-10-22T16:01:00Z"), inspector(3, state="UNKNOWN")]
    entries = list(zip(records, reports, strict=True)) + [(feedback(), inspector(4))]
    path = bundle(tmp_path, entries)
    manifest = json.loads(path.read_text())
    manifest["samples"].append(manifest["samples"][0])
    write(path, manifest)
    result = summarize(path)
    live = result["groups"]["authorized_runtime"]
    assert result["duplicate_records_ignored"] == 1
    assert result["groups"]["local_synthetic"]["feedback_records"] == 1
    assert live["feedback_records"] == 3 and live["reported_natural_day_count"] == 2
    assert live["phone"]["received"] == 2 and live["phone"]["missing_confirmations"] == 1
    assert (live["phone"]["p50_ms"], live["phone"]["p95_ms"]) == (1000, 2000)
    assert live["model"]["states"] == {"SETTLED": 2, "UNKNOWN": 1}
    assert live["model"]["prompt_tokens"] == {"observed_n": 2, "missing_n": 1, "sum": 20}
    assert live["model"]["accounted_or_reserved_micro_usd"]["sum"] == 20
    assert live["model"]["reservation_micro_usd"]["sum"] == 30
    assert live["model"]["supplier_invoice_micro_usd"]["reported_sum"] == 11
    assert not live["model"]["supplier_invoice_micro_usd"]["allocation_verified"]
    assert live["reported_incidents"] is None and not live["cross_day_runtime_quality_proven"]
    assert live["coverage_observations"] == {"reply:ID_ONLY": 2}


@pytest.mark.parametrize("change", ["hash", "trace", "candidate", "unknown", "conflict", "request"])
def test_mismatched_or_double_counted_evidence_is_refused(tmp_path, change):
    entries = [(feedback(), inspector(1))]
    if change in {"conflict", "request"}:
        second = copy.deepcopy(entries[0][0])
        second["reason_private"] = "different review"
        report = inspector(1 if change == "conflict" else 2)
        if change == "request":
            report["trace"]["jobs"][0]["model_calls"][0]["request_ref_sha256"] = (
                entries[0][1]["trace"]["jobs"][0]["model_calls"][0]["request_ref_sha256"])
        entries.append((second, report))
    path = bundle(tmp_path, entries)
    if change not in {"conflict", "request"}:
        report_path = tmp_path / "sample-0.inspector.json"
        report = json.loads(report_path.read_text())
        if change == "trace":
            report["trace_ref_sha256"] = "f" * 64
        elif change == "candidate":
            report["configured_candidate_sha"] = "f" * 40
        elif change == "unknown":
            report["trace"]["jobs"][0]["model_calls"][0]["state"] = "UNKNOWN"
        else:
            report["blockers"] = ["PRIVATE_FAILURE"]
        changed = write(report_path, report)
        if change != "hash":
            record_path = tmp_path / "sample-0.feedback.json"
            record = json.loads(record_path.read_text())
            record["inspector_sha256"] = changed
            write(record_path, record)
    with pytest.raises(ValueError):
        summarize(path)


def test_single_pair_and_unobserved_template_do_not_fabricate_percentiles(tmp_path):
    record = feedback()
    phone_pair(record, 1)
    result = summarize(bundle(tmp_path, [(record, inspector(1)), (feedback("NOT_RUN"), None)]))
    group = result["groups"]["local_synthetic"]
    assert group["phone"]["paired_e2e_n"] == 1 and group["phone"]["p95_ms"] is None
    empty = result["groups"]["NOT_RUN"]
    assert empty["reported_natural_day_count"] is None
    assert empty["model"]["attempt_records"] is None
    assert empty["model"]["supplier_invoice_micro_usd"]["reported_sum"] is None


@pytest.mark.parametrize("manual_first", [True, False])
def test_manual_feedback_and_later_inspector_for_same_trace_cannot_double_count(
    tmp_path, manual_first
):
    report = inspector(1)
    # The real Inspector's event and trace hashes represent distinct identifiers.
    report["trace"]["event_ref_sha256"] = "f" * 64
    manual, inspected = feedback(), feedback()
    for record in (manual, inspected):
        record["trace_ref_sha256"] = report["trace_ref_sha256"]
        phone_pair(record, 1)
    entries = [(manual, None), (inspected, report)]
    if not manual_first:
        entries.reverse()
    with pytest.raises(ValueError, match="CONFLICTING_TRACE_FEEDBACK"):
        summarize(bundle(tmp_path, entries))


def test_stop_summary_keeps_missing_component_distinct_from_both_confirmed(tmp_path):
    partial, complete, unobserved = feedback(), feedback(), feedback()
    partial["stop_receipts"]["core"] = "CONFIRMED"
    complete["stop_receipts"] = {"core": "CONFIRMED", "bridge": "CONFIRMED"}
    result = summarize(bundle(tmp_path, [(partial, inspector(1)), (complete, inspector(2)),
                                        (unobserved, inspector(3))]))
    stops = result["groups"]["local_synthetic"]["stop_observations"]
    assert stops["checked_samples"] == 2 and stops["both_confirmed_samples"] == 1
    assert stops["partial_samples"] == 1 and stops["not_run_samples"] == 1
    assert stops["unconfirmed_samples"] == 0
    assert stops["component_states"]["bridge"] == {"NOT_RUN": 2, "CONFIRMED": 1}


def test_optional_feedback_fields_require_consistent_phone_and_type_evidence():
    record = feedback()
    assert validate_record(record)["schema_valid"]
    record["coverage_observations"]["image"] = "ANALYZED"
    assert not validate_record(record)["schema_valid"]
    record["qq_types"] = ["image"]
    assert validate_record(record)["schema_valid"]
    record["phone"].update(received=True, visible_reply_count=2)
    assert not validate_record(record)["schema_valid"]
    record["phone"]["duplicate_check_evidence_sha256"] = "a" * 64
    assert not validate_record(record)["schema_valid"]
    record["incidents"] = ["duplicate_visible_reply"]
    assert validate_record(record)["schema_valid"]


def test_private_cli_rejects_escaping_paths_without_disclosing_values(tmp_path):
    path = bundle(tmp_path, [(feedback(), None)])
    manifest = json.loads(path.read_text())
    manifest["samples"][0]["feedback"] = "../PRIVATE_ACCOUNT_SECRET.json"
    write(path, manifest)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT)}
    completed = subprocess.run([sys.executable, "-B", "-m", "scripts.summarize_beta_feedback",
                                str(path)], cwd=ROOT, env=env, capture_output=True, text=True)
    assert completed.returncode == 2 and completed.stderr == ""
    assert json.loads(completed.stdout)["state"] == "INVALID_PRIVATE_OBSERVATION_SET"
    assert "PRIVATE_" not in completed.stdout.replace("INVALID_PRIVATE_OBSERVATION_SET", "")


@pytest.mark.parametrize("change", ["duplicate_key", "manifest_cap", "feedback_cap", "boolean"])
def test_unbounded_or_ambiguous_private_inputs_are_not_read_as_observations(tmp_path, change):
    path = bundle(tmp_path, [(feedback(), None)])
    if change == "duplicate_key":
        path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    elif change == "manifest_cap":
        path.write_bytes(b" " * 131073)
    elif change == "feedback_cap":
        (tmp_path / "sample-0.feedback.json").write_bytes(b" " * 65537)
    else:
        manifest = json.loads(path.read_text())
        manifest["schema_version"] = True
        write(path, manifest)
    with pytest.raises(ValueError):
        summarize(path)


def test_private_beta_configuration_and_baseline_never_approve_runtime():
    path = ROOT / "docs/qq-beta"
    raw = tomllib.loads((path / "p12-private-beta.example.toml").read_text("utf-8"))
    required = json.loads((path / "p12-gates.json").read_text("utf-8"))
    cfg = Settings(**raw, owner_qq_id="10001", bot_qq_id="10002", bridge_token="synthetic-" * 4)
    for name in required["private_beta_required_settings"]:
        assert raw[name] is False and getattr(cfg, name) is False
    assert cfg.paid_model is cfg.live_text_window is None
    assert required["historical_runtime"]["current_state"] == "NOT_RECHECKED"
    assert all(value is False for value in required["private_beta_required_settings"].values())
    assert all(value is None for value in required["new_runtime_authority"].values())
    assert required["private_beta_ready"] is False and required["production_ready"] is False
    for package in required["packages"].values():
        assert package["status"] in {"NOT_RUN", "DONE", "OFFLINE_PASS", "BLOCKED"}
        assert package["new_runtime_approval"] is None
        evidence = package["evidence"]
        assert evidence["live_platform"] == "NOT_RUN"
        assert evidence["phone_confirmation"] is None
        assert evidence["cross_natural_days"] is None
        assert evidence["release"] == "DISABLED"
        assert (path / package["evidence_document"]).is_file()


@pytest.mark.parametrize("section,field,value", [
    ("root", "private_beta_ready", True),
    ("root", "production_ready", True),
    ("package", "new_runtime_approval", "a" * 64),
    ("evidence", "live_platform", "LIMITED_LIVE_PASS"),
    ("evidence", "phone_confirmation", "CONFIRMED"),
    ("evidence", "cross_natural_days", 1),
    ("evidence", "release", "ENABLED"),
])
def test_p12_documentation_check_rejects_promoted_runtime(
    tmp_path, monkeypatch, section, field, value,
):
    source = ROOT / "docs/qq-beta"
    target = tmp_path / "docs/qq-beta"
    target.mkdir(parents=True)
    gates = json.loads((source / "p12-gates.json").read_text("utf-8"))
    for package in gates["packages"].values():
        (target / package["evidence_document"]).touch()
    package = gates["packages"]["A1"]
    changed = {"root": gates, "package": package, "evidence": package["evidence"]}[section]
    changed[field] = value
    write(target / "p12-gates.json", gates)
    (target / "p12-private-beta.example.toml").write_bytes(
        (source / "p12-private-beta.example.toml").read_bytes(),
    )
    monkeypatch.setitem(test_private_beta_configuration_and_baseline_never_approve_runtime.__globals__,
                        "ROOT", tmp_path)
    with pytest.raises(AssertionError):
        test_private_beta_configuration_and_baseline_never_approve_runtime()
