"""Cache presence is observation metadata, independent of accounting defaults."""

import json
from pathlib import Path

import httpx
import pytest

from companion import beta_observation, budget, cognition_read
from companion.agent_messages import to_runtime_response
from companion.contracts import CoreError
from companion.model import ModelClient, ModelResponse, ModelResponseError
from companion.store import encode
from tests.conftest import run
from tests.owner_model_fixtures import authorized_job
from tests.support.paid_model import approved_live

MESSAGES = [{"role": "user", "content": "synthetic cache observation"}]
ABSENT = object()


def attempt(core, *, cache=ABSENT, usage=ABSENT, status=200, number=1, marker=ABSENT):
    request_id = budget.reserve(core.store, core.settings, authorized_job(core, number=number),
                                "chat", MESSAGES, core.clock())
    body = {"choices": [{"message": {"content": "synthetic reply"}}]}
    if usage is ABSENT:
        usage = {"prompt_tokens": 80, "completion_tokens": 3}
        if cache is not ABSENT:
            usage["prompt_cache_hit_tokens"] = cache
    if usage != "NO_USAGE":
        body["usage"] = usage
    calls = []

    class Client(ModelClient):
        async def complete(self, *args, **kwargs):
            result = await super().complete(*args, **kwargs)
            if marker is not ABSENT:
                result.cache_usage_present = marker
            return result

    async def exercise():
        def provider(request):
            calls.append(json.loads(request.content))
            return httpx.Response(status, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            try:
                return await budget.complete(Client(core.settings, client), core.store,
                    core.settings, request_id, MESSAGES, "chat", core.clock)
            except CoreError as error:
                return error

    response = run(exercise())
    assert len(calls) == 1
    row = core.store.one("SELECT * FROM model_usage WHERE request_id=?", (request_id,))
    record = core.store.one("SELECT * FROM audit_log WHERE action='model.usage_diagnostic' "
                            "AND entity_id=?", (request_id,))
    return response, row, record


def projections(core, row):
    item = cognition_read.CognitionRead(core).page("model", record_id=row["request_id"])["items"][0]
    trace = core.store.one("SELECT e.trace_id FROM jobs j JOIN events e USING(event_id) "
                           "WHERE j.job_id=?", (row["job_id"],))["trace_id"]
    inspected = beta_observation.inspect_context(core.settings, trace)
    calls = inspected["trace"]["jobs"][0]["model_calls"]
    call = next(c for c in calls if c["request_ref_sha256"] == beta_observation._hash(
        row["request_id"]))
    assert item["usage"]["cache_usage"] == call["cache_usage"]
    assert call["prompt_cache_hit_tokens"] == call["cache_usage"]["cached_input_tokens"]
    assert item["usage"]["observed"] is True
    assert (call["prompt_tokens"], call["completion_tokens"],
            call["configured_price_micro_usd"]) == (80, 3, row["billed_micro_usd"])
    assert item["usage"]["cached_input_tokens"] == row["cached_input_tokens"]
    return call["cache_usage"]


@pytest.mark.parametrize("cache,present,tokens,price", [
    (ABSENT, False, None, 86), (0, True, 0, 86), (40, True, 40, 50),
])
@pytest.mark.parametrize("status", [200, 400, 429, 503])
def test_actual_model_and_budget_preserve_presence_and_original_pricing(
    core, tmp_path, cache, present, tokens, price, status,
):
    approved_live(core, tmp_path)
    response, row, record = attempt(core, cache=cache, status=status)
    if status >= 400:
        assert isinstance(response, ModelResponseError)
        response = response.response
    assert response.cache_usage_present is present
    assert row["state"] == "SETTLED" and row["billed_micro_usd"] == price
    assert (row["input_tokens"], row["output_tokens"], row["cached_input_tokens"]) == (
        80, 3, tokens or 0)
    assert json.loads(record["safe_detail_json"])["cache_usage_present"] is present
    sdk = to_runtime_response(response, model_name="synthetic", provider_name="synthetic")
    assert sdk.usage.cache_read_tokens == (tokens or 0)
    assert projections(core, row) == {"state": "NOT_RECORDED" if tokens is None else "RECORDED",
                                      "cached_input_tokens": tokens}
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")


@pytest.mark.parametrize("cache", [True, "40", -1, 81])
def test_invalid_cache_values_remain_unknown_liabilities(core, tmp_path, cache):
    approved_live(core, tmp_path)
    response, row, record = attempt(core, cache=cache)
    assert response.cache_usage_present is True
    assert row["state"] == "UNKNOWN"
    assert row["billed_micro_usd"] > 0
    assert json.loads(record["safe_detail_json"])["actual_usage_known"] is False
    assert core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")


@pytest.mark.parametrize("usage,present", [
    ("NO_USAGE", None), (None, None), ([], None), ({}, False),
    ({"prompt_tokens": 80}, False),
    ({"prompt_tokens": True, "completion_tokens": 3, "prompt_cache_hit_tokens": 0}, True),
])
@pytest.mark.parametrize("status", [200, 503])
def test_absent_or_bad_usage_retains_unknown_and_http_error_contract(
    core, tmp_path, usage, present, status,
):
    approved_live(core, tmp_path)
    response, row, record = attempt(core, usage=usage, status=status)
    if status == 503:
        assert isinstance(response, ModelResponseError) == (usage != "NO_USAGE")
        if isinstance(response, ModelResponseError):
            assert response.response.cache_usage_present is present
    else:
        assert response.cache_usage_present is present
    assert row["state"] == "UNKNOWN" and row["billed_micro_usd"] > 0
    assert json.loads(record["safe_detail_json"])["cache_usage_present"] is present


@pytest.mark.parametrize("marker", [None, 1, "true", False])
def test_untrusted_or_manual_marker_cannot_change_valid_settlement(core, tmp_path, marker):
    approved_live(core, tmp_path)
    _, row, record = attempt(core, cache=40, marker=marker)
    manual = ModelResponse("ok", 80, 3, 40, (), "stop")
    assert manual.cache_usage_present is None
    assert row["state"] == "SETTLED" and row["billed_micro_usd"] == 50
    assert json.loads(record["safe_detail_json"])["cache_usage_present"] is (
        marker if type(marker) is bool else None)
    assert projections(core, row)["state"] == "NOT_RECORDED"


@pytest.mark.parametrize("damage", [
    "missing", "old", "corrupt", "oversized", "array", "duplicate_row", "conflict_row",
    "duplicate_key", "nonfinite", "deep", "wrong_request", "input", "output", "cached", "price",
    "known", "marker",
])
def test_ambiguous_original_diagnostic_only_hides_cache(core, tmp_path, damage):
    approved_live(core, tmp_path)
    _, row, record = attempt(core, cache=40)
    detail = json.loads(record["safe_detail_json"])
    raw = record["safe_detail_json"]
    if damage in {"missing", "wrong_request"}:
        core.store.execute("DELETE FROM audit_log WHERE audit_id=?", (record["audit_id"],))
        if damage == "wrong_request":
            core.store.audit("model.usage_diagnostic", "other-request", "budget", core.clock(),
                             **detail)
    elif damage in {"duplicate_row", "conflict_row"}:
        if damage == "conflict_row":
            detail["cache_usage_present"] = False
        core.store.audit("model.usage_diagnostic", row["request_id"], "budget", core.clock(),
                         **detail)
    else:
        if damage == "old":
            del detail["cache_usage_present"]
        elif damage in {"input", "output", "cached", "price"}:
            key = {"input": "input_actual_tokens", "output": "output_actual_tokens",
                   "cached": "cached_input_tokens", "price": "usage_priced_micro_usd"}[damage]
            detail[key] += 1
        elif damage == "known":
            detail["actual_usage_known"] = 1
        elif damage == "marker":
            detail["cache_usage_present"] = 1
        raw = {"corrupt": "{bad", "oversized": "x" * 16385, "array": "[]",
               "nonfinite": raw[:-1] + ',"extra":NaN}', "deep": "[" * 2000 + "]" * 2000,
               "duplicate_key": raw[:-1] + ',"cache_usage_present":true}'}.get(
                   damage, encode(detail))
        # Model storage corruption explicitly; normal writes reject invalid JSON.
        core.store.execute("PRAGMA ignore_check_constraints=ON")
        try:
            core.store.execute("UPDATE audit_log SET safe_detail_json=? WHERE audit_id=?",
                               (raw, record["audit_id"]))
        finally:
            core.store.execute("PRAGMA ignore_check_constraints=OFF")
    before = list(core.store.db.iterdump())
    ledger = Path(core.settings.paid_model.journal_path).read_bytes()
    assert projections(core, row) == {"state": "NOT_RECORDED", "cached_input_tokens": None}
    assert list(core.store.db.iterdump()) == before
    assert Path(core.settings.paid_model.journal_path).read_bytes() == ledger
