"""Synthetic diagnostic records: no platform, approval or provider evidence."""

import json

import pytest

from companion import beta_observation, paid_budget
from companion.privacy import advance_epoch, epoch
from companion.store import encode
from scripts.summarize_beta_feedback import snapshot, summarize
from scripts.validate_beta_feedback import validate_record
from tests.conftest import ingest, run
from tests.support.beta_observation import feedback_template, paid_fixture
from tests.support.p12_beta_operations import bundle, feedback, inspector


def selection_fixture(core):
    ingest(core)
    run(core.run_once("worker"))
    paid_fixture(core)
    core.store.execute("UPDATE model_usage SET scenario='memory_review'")
    job = core.store.one("SELECT job_id,event_id FROM jobs")
    request = core.store.one("SELECT request_id FROM model_usage")["request_id"]
    candidates = [{"memory_id": f"PRIVATE_MEMORY_{i}", "subject": "user",
                   "evidence_sha256": str(i) * 64, "content": "PRIVATE_FACT"}
                  for i in range(3)]
    record = {"privacy_epoch": epoch(core.store), "event_id": job["event_id"],
              "plan_sha256": "a" * 64, "candidates": candidates,
              "candidates_sha256": paid_budget.sha(candidates), "history_sources": [],
              "history_sources_sha256": paid_budget.sha([]),
              "review_payload_sha256": "b" * 64, "selected": candidates[:2],
              "selected_sha256": paid_budget.sha(candidates[:2]), "review_request_id": request}
    context = {"privacy_epoch": epoch(core.store), "event_id": job["event_id"],
               "plan_sha256": "a" * 64, "payload_sha256": "c" * 64,
               "sources": [], "memory_sources": candidates[:1]}
    core.store.insert("runtime_meta", key="live_text_context:" + job["job_id"],
                      value=encode(context))
    core.store.insert("runtime_meta", key="live_text_memory:" + job["job_id"], value=encode(record))
    return job, record


def inspect(core):
    return beta_observation.inspect_context(core.settings, "trace_test_1")


def test_inspector_explains_recorded_selection_without_reconstructing_private_reason(core):
    selection_fixture(core)
    before = list(core.store.db.iterdump())
    result = inspect(core)
    selected = result["trace"]["jobs"][0]["memory_selection"]
    assert selected["state"] == "SELECTION_RECORDED"
    assert (selected["candidate_count"], selected["selected_count"],
            selected["frozen_context_count"]) == (3, 2, 1)
    assert [row["disposition"] for row in selected["candidates"]] == [
        "IN_FROZEN_CHAT_CONTEXT", "NOT_IN_FROZEN_CHAT_CONTEXT", "NOT_SELECTED_BY_RECORDED_REVIEW",
    ]
    assert selected["selection_reason"] == selected["discarded_history"] == "NOT_RECORDED"
    assert selected["source_validity"] == "NOT_REVALIDATED"
    assert "PRIVATE_" not in encode(result)
    assert list(core.store.db.iterdump()) == before
    advance_epoch(core.store)
    result = inspect(core)["trace"]["jobs"][0]
    assert result["memory_selection"]["state"] == "STALE_EPOCH"
    assert "candidates" not in result["memory_selection"]
    assert "memory" not in result["context"]
    core.store.execute("UPDATE events SET redacted=1")
    assert inspect(core)["trace"]["jobs"][0]["memory_selection"]["state"] == "REDACTED"


@pytest.mark.parametrize("damage", ["hash", "event", "duplicate", "foreign_selection",
                                     "unsettled", "payload", "invalid", "oversized"])
def test_inspector_suppresses_invalid_or_unbound_selection(core, damage):
    job, record = selection_fixture(core)
    if damage == "hash":
        record["candidates_sha256"] = "f" * 64
    elif damage == "event":
        record["event_id"] = "other-private-event"
    elif damage == "duplicate":
        record["candidates"].append(record["candidates"][0])
        record["candidates_sha256"] = paid_budget.sha(record["candidates"])
    elif damage == "foreign_selection":
        record["selected"] = [{"memory_id": "foreign"}]
        record["selected_sha256"] = paid_budget.sha(record["selected"])
    elif damage == "unsettled":
        core.store.execute("UPDATE model_usage SET state='UNKNOWN'")
    elif damage == "payload":
        record["review_payload_sha256"] = "d" * 64
    raw = "[" if damage == "invalid" else "x" * 131073 if damage == "oversized" else encode(record)
    core.store.execute('UPDATE runtime_meta SET value=%s WHERE key=%s',
                       (raw, "live_text_memory:" + job["job_id"]))
    result = inspect(core)
    assert not result["blockers"]
    diagnostic = result["trace"]["jobs"][0]["memory_selection"]
    assert diagnostic == {"state": "INCONSISTENT", "source_validity": "NOT_REVALIDATED"}


def test_inspector_distinguishes_pending_empty_and_absent_selection(core):
    job, record = selection_fixture(core)
    core.store.execute('DELETE FROM runtime_meta WHERE key=%s',
                       ("live_text_context:" + job["job_id"],))
    record.update(selected=None, selected_sha256=None, review_request_id=None)
    for state in ("REVIEW_PENDING", "NO_CANDIDATES"):
        core.store.execute('UPDATE runtime_meta SET value=%s WHERE key=%s',
                           (encode(record), "live_text_memory:" + job["job_id"]))
        result = inspect(core)["trace"]["jobs"][0]["memory_selection"]
        assert result["state"] == state
        assert result["frozen_context_count"] is None
        record.update(candidates=[], candidates_sha256=paid_budget.sha([]), selected=[],
                      selected_sha256=paid_budget.sha([]), review_payload_sha256=None)
    core.store.execute('DELETE FROM runtime_meta WHERE key=%s',
                       ("live_text_memory:" + job["job_id"],))
    assert inspect(core)["trace"]["jobs"][0]["memory_selection"]["state"] == "NOT_RECORDED"


def test_runtime_feedback_requires_trace_candidate_inspector_and_approval_refs():
    record = feedback_template()
    record["evidence_type"] = "authorized_runtime"
    assert not validate_record(record)["schema_valid"]
    record.update(trace_ref_sha256="a" * 64, inspector_sha256="b" * 64, candidate_sha="c" * 40)
    record["authorization_refs"]["R3"] = "d" * 64
    assert validate_record(record)["schema_valid"]
    record["qq_types"] = ["image"]
    record["coverage_observations"] = {"image": "ANALYZED"}
    assert not validate_record(record)["schema_valid"]
    record["authorization_refs"]["media"] = "e" * 64
    assert validate_record(record)["schema_valid"]


def test_feedback_rejects_inspector_with_different_approval(core):
    ingest(core)
    run(core.run_once("worker"))
    report = inspect(core)
    record = feedback_template()
    record.update(trace_ref_sha256=report["trace_ref_sha256"], candidate_sha="c" * 40)
    record["authorization_refs"]["R3"] = "d" * 64
    report["configured_approval_sha256"] = "e" * 64
    raw = json.dumps(report).encode()
    record["inspector_sha256"] = beta_observation.hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="APPROVAL_MISMATCH"):
        snapshot(record, report, raw)


@pytest.mark.parametrize("scope,error", [("R4", "PAID_APPROVAL_REQUIRED"),
                                        ("R5", "MEMORY_APPROVAL_REQUIRED"),
                                        ("media", "MEDIA_APPROVAL_REQUIRED")])
def test_runtime_summary_requires_refs_for_recorded_effects(tmp_path, scope, error):
    record, report = feedback("authorized_runtime"), inspector(1)
    report["trace"]["jobs"][0]["context"] = {
        "memory": [{"memory_ref_sha256": "a" * 64}], "images": [{"media_sha256": "b" * 64}],
    }
    record["authorization_refs"].update(R4="d" * 64, R5="e" * 64, media="f" * 64)
    record["authorization_refs"][scope] = None
    with pytest.raises(ValueError, match=error):
        summarize(bundle(tmp_path, [(record, report)]))


def test_incomplete_runtime_feedback_and_optional_ratings_remain_unknown(tmp_path):
    record = feedback("authorized_runtime")
    record.update(trace_ref_sha256="a" * 64, candidate_sha="b" * 40)
    with pytest.raises(ValueError, match="FEEDBACK_INVALID"):
        summarize(bundle(tmp_path, [(record, None)]))
    record = feedback()
    record["ratings"].update(diary_authenticity=4, interest_spontaneity=None)
    group = summarize(bundle(tmp_path, [(record, None)]))["groups"]["local_synthetic"]
    assert group["ratings"]["diary_authenticity"] == {"n": 1, "mean": 4}
    assert group["ratings"]["interest_spontaneity"] == {"n": 0, "mean": None}
    assert group["ratings"]["privacy_comfort"] == {"n": 0, "mean": None}


def test_runtime_feedback_cannot_attribute_old_event_to_current_config(tmp_path):
    record, report = feedback("authorized_runtime"), inspector(1)
    report["trace"]["recorded_window"] = {
        "state": "RECORDED", "plan_sha256": "a" * 64, "approval_sha256": "f" * 64,
        "candidate_sha": "c" * 40, "execution": "approved_live",
    }
    # Current configuration matches feedback but the event belongs to a different approval.
    with pytest.raises(ValueError, match="EVENT_APPROVAL_MISMATCH"):
        summarize(bundle(tmp_path, [(record, report)]))


def test_inspector_reads_original_input_window_and_hides_erased_bindings(core):
    ingest(core)
    run(core.run_once("worker"))
    original = {"state": "OPEN", "approval": {
        "plan_sha256": "a" * 64, "approval_sha256": "b" * 64,
        "candidate_sha": "c" * 40, "execution": "local_synthetic",
    }}
    original["binding"] = {"plan_sha256": "a" * 64, "privacy_epoch": epoch(core.store),
                           "approval_sha256": paid_budget.sha(original["approval"])}
    core.store.insert("runtime_meta", key="live_text_window:" + "a" * 64, value=encode(original))
    core.store.insert("runtime_meta", key="live_text_input:evt_test_1", value=encode({
        "event_id": "evt_test_1", "window_sha256": "a" * 64, "privacy_epoch": epoch(core.store),
        "capture": {"signature": "PRIVATE_SIGNATURE", "source_event_id": "PRIVATE_QQ_ID"},
    }))
    report = inspect(core)
    binding = report["trace"]["recorded_window"]
    assert binding["state"] == "RECORDED"
    assert binding["plan_sha256"] == "a" * 64
    assert binding["approval_sha256"] == "b" * 64 and binding["candidate_sha"] == "c" * 40
    assert report["configured_approval_sha256"] is None
    assert binding["source_validity"] == "NOT_REVALIDATED"
    assert "PRIVATE_" not in encode(report)
    advance_epoch(core.store)
    binding = inspect(core)["trace"]["recorded_window"]
    assert binding == {"state": "STALE_EPOCH", "source_validity": "NOT_REVALIDATED"}
    core.store.execute("UPDATE events SET redacted=1")
    assert inspect(core)["trace"]["recorded_window"]["state"] == "REDACTED"


@pytest.mark.parametrize("state", ["NOT_RECORDED", "STALE_EPOCH", "REDACTED"])
@pytest.mark.parametrize("scenario,error", [("memory_review", "MEMORY_APPROVAL_REQUIRED"),
                                           ("content_vision", "MEDIA_APPROVAL_REQUIRED")])
def test_hidden_context_does_not_remove_used_capability_approval(tmp_path, state, scenario, error):
    record, report = feedback("authorized_runtime"), inspector(1)
    job = report["trace"]["jobs"][0]
    job["context"] = {"state": state}
    job["model_calls"][0]["scenario"] = scenario
    with pytest.raises(ValueError, match=error):
        summarize(bundle(tmp_path, [(record, report)]))
