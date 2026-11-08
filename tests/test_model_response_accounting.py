"""Received usage survives an unusable reply without turning it into a reply."""

import json
from pathlib import Path

import httpx
import pytest

from companion import budget
from companion.contracts import CoreError
from companion.model import ModelClient
from tests.conftest import run
from tests.owner_model_fixtures import authorized_job
from tests.support.paid_model import approved_live

MESSAGES = [{"role": "user", "content": "synthetic accounting boundary"}]


def rejected_response(core, response):
    request_id = budget.reserve(
        core.store, core.settings, authorized_job(core), "chat", MESSAGES, core.clock(),
    )
    received = []

    async def scenario():
        def provider(request):
            received.append(request)
            return response

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            with pytest.raises(CoreError) as caught:
                await budget.complete(ModelClient(core.settings, client), core.store, core.settings,
                                      request_id, MESSAGES, "chat", core.clock)
            before = core.store.one('SELECT * FROM model_usage WHERE request_id=%s', (request_id,))
            # A duplicate recovery callback cannot discard known usage or reduce
            # the amount already settled by the calling seam.
            budget.settle(core.store, core.settings, request_id)
            after = core.store.one('SELECT * FROM model_usage WHERE request_id=%s', (request_id,))
            assert after == before
            return caught.value.code, after

    code, usage = run(scenario())
    assert len(received) == 1
    assert not core.store.rows("SELECT * FROM outbox")
    return code, usage


@pytest.mark.parametrize("choices,expected_code", [
    ([{"message": {"content": None}}], "MODEL_INVALID_RESPONSE"),
    ([{"message": {"content": " "}}], "MODEL_EMPTY_RESPONSE"),
    ([{"message": {"content": "x" * 32001}}], "MODEL_INVALID_RESPONSE"),
    ([], "MODEL_INVALID_RESPONSE"),
])
def test_invalid_reply_preserves_large_known_usage_and_blocks_new_io(
    core, tmp_path, choices, expected_code,
):
    approval = approved_live(core, tmp_path)
    code, usage = rejected_response(core, httpx.Response(200, json={
        "choices": choices,
        "usage": {"prompt_tokens": 50000, "completion_tokens": 20000},
    }))
    assert code == expected_code
    assert usage["state"] == "UNKNOWN"
    assert (usage["input_tokens"], usage["output_tokens"]) == (50000, 20000)
    assert usage["billed_micro_usd"] == 90000
    records = [json.loads(line) for line in Path(approval.journal_path).read_text().splitlines()]
    assert records[-1]["minimum_micro_usd"] >= 90000
    assert any(row["kind"] == "blocked" for row in records)
    with pytest.raises(CoreError, match="MODEL_OUTCOME_UNKNOWN"):
        budget.reserve(core.store, core.settings, authorized_job(core),
            "chat", MESSAGES, core.clock())


@pytest.mark.parametrize("status,code", [
    (200, "MODEL_INVALID_RESPONSE"), (400, "MODEL_REJECTED"),
    (429, "MODEL_RETRYABLE"), (503, "MODEL_RETRYABLE"),
])
def test_unusable_response_with_known_usage_within_limits_settles_and_keeps_error(
    core, tmp_path, status, code,
):
    approved_live(core, tmp_path)
    actual_code, usage = rejected_response(core, httpx.Response(status, json={
        "error": "synthetic provider error",
        "usage": {"prompt_tokens": 80, "completion_tokens": 3, "prompt_cache_hit_tokens": 40},
    }))
    assert actual_code == code
    assert usage["state"] == "SETTLED"
    assert (usage["input_tokens"], usage["output_tokens"], usage["cached_input_tokens"]) == (
        80, 3, 40,
    )
    assert usage["billed_micro_usd"] == 50
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")
    budget.reserve(core.store, core.settings, authorized_job(core), "chat", MESSAGES, core.clock())


@pytest.mark.parametrize("status", [400, 429, 503])
def test_error_http_preserves_large_known_usage_and_stops_new_io(core, tmp_path, status):
    approved_live(core, tmp_path)
    _, usage = rejected_response(core, httpx.Response(status, json={
        "error": "synthetic provider error",
        "usage": {"prompt_tokens": 50000, "completion_tokens": 20000},
    }))
    assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] == 90000
    assert core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")


@pytest.mark.parametrize("status,usage", [
    (200, None), (200, {"prompt_tokens": True, "completion_tokens": 3}),
    (200, {"prompt_tokens": 80}), (503, {"prompt_tokens": -1, "completion_tokens": 3}),
])
def test_invalid_reply_with_missing_or_invalid_explicit_usage_stops_new_io(
    core, tmp_path, status, usage,
):
    approved_live(core, tmp_path)
    _, recorded = rejected_response(core, httpx.Response(status, json={
        "choices": [{"message": {"content": None}}], "usage": usage,
    }))
    assert recorded["state"] == "UNKNOWN"
    assert core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")


def test_non_json_success_is_a_returned_invalid_response_not_a_transport_retry(core, tmp_path):
    approved_live(core, tmp_path)
    code, usage = rejected_response(core, httpx.Response(200, content=b"not JSON"))
    assert code == "MODEL_INVALID_RESPONSE" and usage["state"] == "UNKNOWN"
    assert core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")
