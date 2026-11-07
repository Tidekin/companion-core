import json
import sqlite3
import sys
from pathlib import Path

import pytest

from companion import __main__, beta_observation
from companion.core import Core
from companion.privacy import advance_epoch, epoch
from companion.store import encode
from scripts.validate_beta_feedback import validate_record
from tests.conftest import ingest, run
from tests.support.beta_observation import feedback_template, paid_fixture
from tests.support.ops_status import settings


def test_missing_database_and_wrong_owner_never_bootstrap_or_disclose(core, tmp_path):
    cfg = settings(tmp_path / "absent")
    assert beta_observation.inspect_context(cfg, "trace")['blockers'] == ["DATABASE_MISSING"]
    assert not Path(cfg.database).parent.exists()
    ingest(core, text="PRIVATE_OWNER_BODY")
    wrong = core.settings.model_copy(update={"owner_qq_id": "99999"})
    result = beta_observation.inspect_context(wrong, "trace_test_1")
    assert result["blockers"] == ["OWNER_IDENTITY_MISMATCH"]
    assert "trace" not in result


def test_committed_wal_snapshot_does_not_write_or_emit_plaintext(core, monkeypatch):
    ingest(core, text="PRIVATE_BODY_DO_NOT_EMIT")
    run(core.run_once("worker"))
    core.store.execute("UPDATE outbox SET state='DELIVERY_UNCERTAIN'")
    core.store.execute("UPDATE jobs SET last_error_code='PRIVATE_FAILURE_DO_NOT_EMIT'")
    before = list(core.store.db.iterdump())
    ledger = Path(core.settings.erasure_ledger).read_bytes()
    calls = len(core.model.calls)
    connect = sqlite3.connect

    def readonly(*args, **kwargs):
        assert "mode=ro" in args[0] and "immutable" not in args[0]
        return connect(*args, **kwargs)

    monkeypatch.setattr(beta_observation.sqlite3, "connect", readonly)
    result = beta_observation.inspect_context(core.settings, "trace_test_1")
    assert not result["blockers"]
    trace = result["trace"]
    assert trace["outbox"] == {"DELIVERY_UNCERTAIN": 1}
    assert trace["phone_received"] is trace["phone_e2e_ms"] is None
    assert trace["provider_invoice_micro_usd"] is None
    job = trace["jobs"][0]
    assert job["model_attempt_count"] == 1
    assert job["model_calls"][0]["prompt_tokens"] is None  # Demo is not observed paid usage.
    assert job["failure_recorded"]
    output = encode(result)
    for value in ("PRIVATE_BODY", "PRIVATE_FAILURE", "trace_test_1", core.settings.owner_qq_id,
                  core.settings.database, core.settings.bridge_token.get_secret_value()):
        assert value not in output
    assert list(core.store.db.iterdump()) == before
    assert Path(core.settings.erasure_ledger).read_bytes() == ledger
    assert len(core.model.calls) == calls


def test_unknown_usage_never_promoted_to_observed_tokens_or_invoice(core):
    ingest(core)
    run(core.run_once("worker"))
    paid_fixture(core)
    result = beta_observation.inspect_context(core.settings, "trace_test_1")
    call = result["trace"]["jobs"][0]["model_calls"][0]
    assert call["prompt_tokens"] == 100
    # The manual fixture has no original provider cache diagnostic.
    assert call["prompt_cache_hit_tokens"] is None
    assert call["cache_usage"] == {"state": "NOT_RECORDED", "cached_input_tokens": None}
    cached = core.store.one("SELECT cached_input_tokens FROM model_usage")
    assert cached["cached_input_tokens"] == 40
    assert call["completion_tokens"] == 20
    assert call["configured_price_micro_usd"] is not None
    core.store.execute("UPDATE model_usage SET state='UNKNOWN'")
    result = beta_observation.inspect_context(core.settings, "trace_test_1")
    call = result["trace"]["jobs"][0]["model_calls"][0]
    assert call["state"] == "UNKNOWN"
    assert call["prompt_tokens"] is call["configured_price_micro_usd"] is None
    assert call["prompt_cache_hit_tokens"] is None
    assert call["cache_usage"] == {"state": "NOT_RECORDED", "cached_input_tokens": None}
    assert call["accounted_or_reserved_micro_usd"] > 0
    assert call["reservation_micro_usd"] == 100


def test_context_projects_only_pins_and_marks_erasure_epoch_stale(core):
    ingest(core)
    run(core.run_once("worker"))
    job = core.store.one("SELECT job_id FROM jobs")
    record = {"privacy_epoch": epoch(core.store),
              "plan_sha256": "a" * 64, "payload_sha256": "b" * 64,
              "sources": [{"event_id": "PRIVATE_EVENT", "text": "PRIVATE_HISTORY"}],
              "memory_sources": [{"memory_id": "PRIVATE_MEMORY", "subject": "user",
                                  "evidence_sha256": "c" * 64, "content": "PRIVATE_FACT"}]}
    core.store.insert("runtime_meta", key="live_text_context:" + job["job_id"],
                      value=encode(record))
    result = beta_observation.inspect_context(core.settings, "trace_test_1")
    context = result["trace"]["jobs"][0]["context"]
    assert context["state"] == "RECORDED"
    assert context["source_validity"] == "NOT_REVALIDATED"
    assert len(context["history"]) == len(context["memory"]) == 1
    assert "PRIVATE_" not in encode(result)
    advance_epoch(core.store)
    result = beta_observation.inspect_context(core.settings, "trace_test_1")
    assert result["trace"]["jobs"][0]["context"]["state"] == "STALE_EPOCH"
    core.store.execute("UPDATE events SET redacted=1")
    result = beta_observation.inspect_context(core.settings, "trace_test_1")
    assert result["trace"]["state"] == "REDACTED"
    assert result["trace"]["jobs"][0]["context"] == {
        "state": "REDACTED", "source_validity": "NOT_REVALIDATED",
    }


def test_image_inspection_preserves_unknown_cost_and_projects_only_frozen_hashes(core):
    ingest(core, text="PRIVATE_IMAGE_CAPTION")
    run(core.run_once("worker"))
    paid_fixture(core)
    core.store.execute("UPDATE model_usage SET scenario='content_vision'")
    job = core.store.one("SELECT job_id FROM jobs")
    image = {"privacy_epoch": epoch(core.store),
             "event_id": "PRIVATE_IMAGE_EVENT", "item_id": "PRIVATE_IMAGE_ITEM",
             "source": {"source_sha256": "c" * 64, "url": "PRIVATE_SIGNED_URL"},
             "media_sha256": "d" * 64, "payload_sha256": "e" * 64,
             "request_id": "PRIVATE_REQUEST", "text": "PRIVATE_VISION_OUTPUT"}
    record = {"privacy_epoch": epoch(core.store), "sources": [], "memory_sources": [],
              "image_sources": [{"image": image, "item_sha256": "f" * 64,
                                 "observation_sha256": "a" * 64, "admission_sha256": "b" * 64}]}
    core.store.insert("runtime_meta", key="live_text_context:" + job["job_id"],
                      value=encode(record))
    before = list(core.store.db.iterdump())
    result = beta_observation.inspect_context(core.settings, "trace_test_1")
    assert result["configured_scopes"]["image"] == "none"
    inspected = result["trace"]["jobs"][0]
    assert inspected["model_calls"][0]["scenario"] == "content_vision"
    assert inspected["model_calls"][0]["prompt_tokens"] == 100
    source = inspected["context"]["images"][0]
    assert source["source_validity"] == "NOT_REVALIDATED"
    assert source["media_sha256"] == "d" * 64 and source["observation_sha256"] == "a" * 64
    assert "PRIVATE_" not in encode(result)
    assert list(core.store.db.iterdump()) == before
    core.store.execute("UPDATE model_usage SET state='UNKNOWN'")
    advance_epoch(core.store)
    result = beta_observation.inspect_context(core.settings, "trace_test_1")
    inspected = result["trace"]["jobs"][0]
    assert inspected["context"]["state"] == "STALE_EPOCH"
    assert inspected["model_calls"][0]["prompt_tokens"] is None
    assert inspected["model_calls"][0]["accounted_or_reserved_micro_usd"] > 0
    core.store.execute("UPDATE events SET redacted=1")
    result = beta_observation.inspect_context(core.settings, "trace_test_1")
    assert "images" not in result["trace"]["jobs"][0]["context"]


def test_ambiguous_and_third_party_trace_not_mislabelled_owner(core):
    ingest(core)
    ingest(core, number=2)
    core.store.execute("UPDATE events SET actor_id='companion' WHERE event_id='evt_test_1'")
    assert beta_observation.inspect_context(core.settings, "trace_test_1")["trace"] == {
        "state": "NOT_FOUND",
    }
    core.store.execute("UPDATE events SET actor_id='owner',trace_id='shared-trace'")
    assert beta_observation.inspect_context(core.settings, "shared-trace")["trace"] == {
        "state": "AMBIGUOUS_TRACE",
    }


@pytest.mark.parametrize("trace", [None, "", "x" * 257])
def test_invalid_trace_rejected_without_database_access(core, trace):
    with pytest.raises(ValueError, match="INVALID_TRACE_ID"):
        beta_observation.inspect_context(core.settings, trace)


def test_cli_is_local_read_only_and_does_not_construct_core(core, monkeypatch, capsys):
    monkeypatch.setattr(__main__.Settings, "load", lambda: core.settings)
    monkeypatch.setattr(Core, "__init__", lambda *_: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(sys, "argv", ["companion", "inspect-context", "--trace-id", "missing"])
    __main__.main()
    result = json.loads(capsys.readouterr().out)
    assert result["trace"] == {"state": "NOT_FOUND"}
    assert result["read_only"] and result["diagnostic_only"]


def test_feedback_unknown_values_do_not_become_real_ratings_or_receipts():
    record = feedback_template()
    result = validate_record(record)
    assert result["schema_valid"]
    assert result["reported_phone_pair_ms"] is None
    assert result["evidence_authenticated"] is result["supplier_invoice_verified"] is False
    assert all(value is None for value in record["ratings"].values())


def test_window_request_template_does_not_grant_or_reset_any_existing_permission():
    path = Path(__file__).resolve().parents[1] / "docs/qq-beta/p11-beta-window.template.json"
    request = json.loads(path.read_text("utf-8"))
    assert request["state"] == "REQUEST_TEMPLATE_NOT_AUTHORIZED"
    assert request["production_ready"] is False
    assert request["starts_at_utc"] is request["ends_at_utc"] is None
    assert not request["periodic_allowance"]["implemented"]
    assert not request["periodic_allowance"]["approved"]
    assert all(not scope["requested"] and scope["evidence_sha256"] is None
               for scope in request["permissions"].values())


@pytest.mark.parametrize("change", ["rating", "phone", "invoice", "timestamp"])
def test_feedback_rejects_fabricated_shape_and_uncalibrated_phone_pair(change):
    record = feedback_template()
    if change == "rating":
        record["ratings"]["naturalness"] = 6
    elif change == "invoice":
        record["provider_invoice"]["micro_usd"] = 123
    elif change == "timestamp":
        record["phone"]["sent_at_utc"] = "not a timestamp"
    else:
        record["phone"].update(sent_at_utc="2026-10-21T12:00:00Z",
                               received_at_utc="2026-10-21T12:00:01Z")
    assert not validate_record(record)["schema_valid"]


def test_feedback_phone_delta_stays_reported_evidence_and_rejects_backwards_time():
    record = feedback_template()
    record["phone"].update(received=True, sent_at_utc="2026-10-21T12:00:00Z",
                           received_at_utc="2026-10-21T12:00:01.25Z",
                           clock_basis="same_device_utc", pair_evidence_sha256="a" * 64)
    result = validate_record(record)
    assert result["reported_phone_pair_ms"] == 1250
    assert result["evidence_authenticated"] is False
    record["phone"]["received_at_utc"] = "2026-10-21T11:59:59Z"
    assert not validate_record(record)["schema_valid"]
