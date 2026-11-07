"""Local estimates are calibrated with usage without claiming a provider cost guarantee."""

import json
import sqlite3
from contextlib import closing

import pytest

from companion import budget, paid_budget
from companion.contracts import CoreError
from companion.model import ModelResponse
from tests.owner_model_fixtures import authorized_job
from tests.support.paid_model import approved_live

MESSAGES = [{"role": "user", "content": "hello"}]


def reserve(core):
    request = budget.reserve(
        core.store, core.settings, authorized_job(core), "chat", MESSAGES, core.clock(),
    )
    return request, core.store.one('SELECT * FROM model_admissions WHERE request_id=%s', (request,))


def diagnostic(core, request):
    row = core.store.one(
        (
            (
                'SELECT safe_detail_json FROM audit_log WHERE '
                "action='model.usage_diagnostic' AND entity_id=%s ORDER BY "
                'record_sequence DESC'
            )
        ), (request,),
    )
    return json.loads(row["safe_detail_json"])


def test_estimate_accepts_local_text_without_a_tokenizer_or_provider_attestation(core, tmp_path):
    approval = approved_live(core, tmp_path)
    assert 1 < paid_budget.estimate_input_tokens(approval, MESSAGES, "chat") < 100
    assert paid_budget.estimate_input_tokens(
        approval, [{"role": "user", "content": "中文与 English mixed text"}], "chat",
    ) > paid_budget.estimate_input_tokens(approval, MESSAGES, "chat")
    assert paid_budget.estimate_input_tokens(approval, MESSAGES, "diary") > (
        paid_budget.estimate_input_tokens(approval, MESSAGES, "chat")
    )
    assert not list(tmp_path.glob("*tokenizer*"))


def test_small_underestimate_settles_actual_usage_and_does_not_stop_the_next_call(core, tmp_path):
    approved_live(core, tmp_path)
    request, admission = reserve(core)
    actual_input = admission["input_bound"] + 10
    budget.settle(core.store, core.settings, request, ModelResponse(
        "ok", actual_input, admission["output_bound"],
    ))
    usage = core.store.one('SELECT * FROM model_usage WHERE request_id=%s', (request,))
    assert usage["state"] == "SETTLED"
    assert usage["billed_micro_usd"] > admission["reserved_micro_usd"]
    assert diagnostic(core, request)["input_error_tokens"] == 10
    assert diagnostic(core, request)["code"] == "NORMAL"
    assert reserve(core)[0] != request


def test_large_input_deviation_is_diagnosed_but_settles_within_local_amount_limits(core, tmp_path):
    approved_live(core, tmp_path)
    request, _ = reserve(core)
    budget.settle(core.store, core.settings, request, ModelResponse("ok", 1000, 2))
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    assert diagnostic(core, request)["code"] == "INPUT_USAGE_SPIKE"
    assert budget.usage_diagnostics(core.store.db)["anomaly_calls"] == 1
    assert reserve(core)[0] != request


@pytest.mark.parametrize("field,code", [
    ("per_attempt_micro_usd", "ATTEMPT_AMOUNT"),
    ("per_request_micro_usd", "REQUEST_AMOUNT"),
    ("daily_micro_usd", "DAILY_AMOUNT"),
    ("monthly_micro_usd", "MONTHLY_AMOUNT"),
    ("window_micro_usd", "WINDOW_AMOUNT"),
    ("local_monthly", "MONTHLY_AMOUNT"),
])
def test_actual_cost_over_a_local_limit_keeps_usage_and_stops_new_io(core, tmp_path, field, code):
    approval = approved_live(core, tmp_path)
    estimated = paid_budget.estimate_input_tokens(approval, MESSAGES, "chat")
    estimate_cost = budget.cost(core.settings, estimated, core.settings.output_tokens("chat"))
    if field == "local_monthly":
        core.settings.monthly_limit_micro_usd = estimate_cost + 5
    else:
        approved_live(core, tmp_path, **{field: estimate_cost + 5})
    request, admission = reserve(core)
    budget.settle(core.store, core.settings, request, ModelResponse(
        "ok", estimated + 10, admission["output_bound"],
    ))
    usage = core.store.one('SELECT * FROM model_usage WHERE request_id=%s', (request,))
    assert usage["state"] == "UNKNOWN"
    assert usage["input_tokens"] == estimated + 10
    assert usage["billed_micro_usd"] == estimate_cost + 10
    assert code in diagnostic(core, request)["limits"]
    assert core.store.one("SELECT value FROM runtime_meta WHERE key='model_budget_blocked'")
    with pytest.raises(CoreError, match="RECONCILIATION|MONTHLY_BUDGET_EXHAUSTED"):
        reserve(core)


def test_output_cap_violation_preserves_known_priced_usage_and_reserved_liability(core, tmp_path):
    approved_live(core, tmp_path)
    request, admission = reserve(core)
    budget.settle(core.store, core.settings, request, ModelResponse(
        "ok", 1, admission["output_bound"] + 1,
    ))
    row = core.store.one("SELECT * FROM model_usage")
    assert row["state"] == "UNKNOWN"
    assert row["output_tokens"] == admission["output_bound"] + 1
    assert row["billed_micro_usd"] >= admission["reserved_micro_usd"]
    assert "OUTPUT_CAP_EXCEEDED" in diagnostic(core, request)["limits"]
    with pytest.raises(CoreError, match="RECONCILIATION"):
        reserve(core)


def test_returned_response_without_usage_stops_even_when_concurrency_remains(core, tmp_path):
    approved_live(core, tmp_path, concurrency=2)
    request, _ = reserve(core)
    budget.settle(core.store, core.settings, request, ModelResponse("ok"))
    assert diagnostic(core, request)["actual_usage_known"] is False
    with pytest.raises(CoreError, match="RECONCILIATION"):
        reserve(core)


def test_transport_unknown_keeps_liability_and_limits_new_native_work(core, tmp_path):
    approved_live(core, tmp_path, concurrency=2)
    request, _ = reserve(core)
    budget.settle(core.store, core.settings, request)
    assert diagnostic(core, request)["code"] == "TRANSPORT_USAGE_UNKNOWN"
    original = core.store.one('SELECT * FROM model_usage WHERE request_id=%s', (request,))
    with pytest.raises(CoreError, match="CLOUD_TASK_USAGE_UNCERTAIN"):
        reserve(core)
    job = authorized_job(core, number=2)
    second = budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())
    assert second != request
    with pytest.raises(CoreError, match="CONCURRENCY"):
        budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())
    assert core.store.one('SELECT * FROM model_usage WHERE request_id=%s', (request,)) == original
    summary = budget.usage_diagnostics(core.store.db)
    assert summary["calibrated_calls"] == summary["input_actual_tokens"] == 0
    assert summary["currency"] is None


def test_calibration_accepts_a_read_only_connection_and_preserves_cached_usage(core, tmp_path):
    approved_live(core, tmp_path)
    request, admission = reserve(core)
    budget.settle(core.store, core.settings, request, ModelResponse("ok", 80, 3, 40))
    with closing(sqlite3.connect(f"file:{core.settings.database}?mode=ro", uri=True)) as readonly:
        summary = budget.usage_diagnostics(readonly)
    assert summary == {
        "calibrated_calls": 1,
        "input_estimated_tokens": admission["input_bound"],
        "input_actual_tokens": 80,
        "output_actual_tokens": 3,
        "cached_input_tokens": 40,
        "input_error_tokens": 80 - admission["input_bound"],
        "reserved_micro_usd": admission["reserved_micro_usd"],
        "usage_priced_micro_usd": 50,
        "cost_error_micro_usd": 50 - admission["reserved_micro_usd"],
        "anomaly_calls": 0,
        "currency": "USD",
        "billing_basis": "configured_rates_not_invoice",
    }


def test_calibration_stays_within_the_status_read_budget_with_a_large_audit_log(tmp_path):
    path = tmp_path / "calibration.db"
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE model_usage (request_id TEXT PRIMARY KEY, state TEXT,
                input_tokens INTEGER, output_tokens INTEGER, cached_input_tokens INTEGER,
                billed_micro_usd INTEGER);
            CREATE TABLE model_admissions (request_id TEXT PRIMARY KEY, input_bound INTEGER,
                reserved_micro_usd INTEGER);
            CREATE TABLE audit_log (action TEXT, entity_id TEXT, safe_detail_json TEXT);
        """)
        db.executemany("INSERT INTO model_usage VALUES (?,?,?,?,?,?)", [
            (str(i), "SETTLED" if i < 512 else ("UNKNOWN" if i == 512 else "RESERVED"),
             110 + i, 3, 2, 11) for i in range(514)])
        db.executemany("INSERT INTO model_admissions VALUES (?,?,?)", [
            (str(i), 100 + i, 10) for i in range(514)])
        db.executemany("INSERT INTO audit_log VALUES (?,?,?)", [
            ("event.accepted", str(i), "{}") for i in range(50000)])
        db.executemany("INSERT INTO audit_log VALUES (?,?,?)", [
            ("model.video_rate_correction", key, "{}") for key in ("0", "0", "1", None)])
        db.commit()

    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as readonly:
        readonly.execute("PRAGMA query_only=ON")
        ticks = 0

        def progress():
            nonlocal ticks
            ticks += 1
            return ticks >= 2000  # Same bounded VM budget as ops_status.snapshot.

        readonly.set_progress_handler(progress, 10000)
        try:
            summary = budget.usage_diagnostics(readonly)
        finally:
            readonly.set_progress_handler(None, 0)
    assert summary == {
        "calibrated_calls": 510,
        "input_estimated_tokens": sum(100 + i for i in range(2, 512)),
        "input_actual_tokens": sum(110 + i for i in range(2, 512)),
        "output_actual_tokens": 1530, "cached_input_tokens": 1020,
        "reserved_micro_usd": 5100, "usage_priced_micro_usd": 5610,
        "input_error_tokens": 5100, "cost_error_micro_usd": 510, "anomaly_calls": 0,
        "currency": "USD", "billing_basis": "configured_rates_not_invoice",
    }
