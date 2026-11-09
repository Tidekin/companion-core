"""Proxy accounting uses original provider usage and never manufactures a charge."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from companion import cloud_budget
from companion.contracts import CoreError
from companion.executive import erase_executive
from companion.executor_budget_proxy import create_budget_proxy
from companion.executor_gateway import receipt
from tests.support.executive import TOKEN, configured, envelope, proposal


@pytest.mark.parametrize("stream", [False, True])
def test_proxy_prices_observed_usage_once_including_stream_final_chunk(stream):
    def provider(request):
        body = json.loads(request.content)
        usage = {"prompt_tokens": 80, "completion_tokens": 3}
        if stream:
            assert body["stream_options"] == {"include_usage": True}
            return httpx.Response(200, content=(
                'data: {"choices":[],"usage":null}\n\n'
                + 'data: ' + json.dumps({"choices": [], "usage": usage})
                + '\n\ndata: [DONE]\n\n'
            ), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [], "usage": usage})

    app = create_budget_proxy(TOKEN, "https://provider.test/v1", "private-key", "model",
                              300_000, 1_200_000, transport=httpx.MockTransport(provider))
    admin = {"Authorization": "Bearer " + TOKEN}
    with TestClient(app) as client:
        token = client.post("/internal/v1/reservations", headers=admin,
                            json=envelope().model_dump()).json()["token"]
        response = client.post("/v1/chat/completions",
                               headers={"Authorization": "Bearer " + token},
                               json={"messages": [], "stream": stream})
        assert response.status_code == 200
        observed = client.get("/internal/v1/reservations/" + token, headers=admin).json()
        assert observed == {"calls": 1, "input_tokens": 80, "output_tokens": 3,
                            "billed_micro_usd": 28, "unknown_calls": 0}


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": True, "completion_tokens": 3},
                                   {"prompt_tokens": -1, "completion_tokens": 3}])
def test_missing_or_invalid_usage_keeps_response_and_unknown_fee(usage):
    app = create_budget_proxy(TOKEN, "https://provider.test/v1", "private-key", "model",
                              300_000, 1_200_000, transport=httpx.MockTransport(lambda _:
                                  httpx.Response(200, json={"choices": [], "usage": usage})))
    admin = {"Authorization": "Bearer " + TOKEN}
    with TestClient(app) as client:
        token = client.post("/internal/v1/reservations", headers=admin,
                            json=envelope().model_dump()).json()["token"]
        assert client.post("/v1/chat/completions", json={"messages": []},
                           headers={"Authorization": "Bearer " + token}).status_code == 200
        observed = client.get("/internal/v1/reservations/" + token, headers=admin).json()
        assert observed["calls"] == observed["unknown_calls"] == 1
        assert observed["billed_micro_usd"] == 0


def test_successful_tool_keeps_result_and_durable_unknown_usage_without_replay(core):
    executive = configured(core)
    core.settings.executive.endpoints["windows"].max_model_calls = 2
    task = executive.submit(proposal())
    leased = executive.claim()
    _, request = executive.begin(leased)
    with core.store.transaction():
        cloud_budget.mark_dispatched(core.store, "tool", task["task_id"], core.clock())
    result = executive.finish(leased, receipt(request, "SUCCEEDED", "original result",
        billed=28, calls=2, usage={"input_tokens": 80, "output_tokens": 3, "unknown_calls": 1}))
    assert result["state"] == "SUCCEEDED" and result["result_event_id"]
    assert result["billed_micro_usd"] == 28
    assert result["last_error_code"] == "MODEL_COST_UNKNOWN"
    snapshot = core.store.one("SELECT * FROM cloud_cost_snapshots")
    assert snapshot["state"] == "UNKNOWN" and snapshot["accounted_micro_usd"] == 28
    original = json.loads(core.store.one(
        "SELECT safe_detail_json FROM audit_log WHERE action='tool.finished'",
    )["safe_detail_json"])
    assert original["input_tokens"] == 80 and original["output_tokens"] == 3
    assert original["unknown_model_calls"] == 1 and original["billed_micro_usd"] == 28
    assert executive.claim() is None
    erase_executive(core.store, ["evt_test_1"])
    core.store.execute("DELETE FROM cloud_cost_snapshots")
    restored = cloud_budget.summary(core.store, core.settings, core.clock())
    assert restored["unknown_micro_cny"] == 224 and restored["settled_micro_cny"] == 0
    assert core.store.one("SELECT state FROM cloud_cost_snapshots")["state"] == "UNKNOWN"


def test_original_tool_usage_survives_erasure_without_accepting_late_content(core):
    executive = configured(core)
    task = executive.submit(proposal())
    leased = executive.claim()
    _, request = executive.begin(leased)
    with core.store.transaction():
        cloud_budget.mark_dispatched(core.store, "tool", task["task_id"], core.clock())
    erase_executive(core.store, ["evt_test_1"])
    with pytest.raises(CoreError):
        executive.finish(leased, receipt(request, "SUCCEEDED", "late private content",
            billed=28, calls=1, usage={"input_tokens": 80, "output_tokens": 3}))
    result = executive.get(task["task_id"])
    assert result["state"] == "CANCELLED" and not result["result_event_id"]
    assert result["billed_micro_usd"] == 28
    assert core.store.one("SELECT * FROM observations") is None
    usage = json.loads(core.store.one(
        "SELECT safe_detail_json FROM audit_log WHERE action='tool.usage_received'",
    )["safe_detail_json"])
    assert usage["input_tokens"] == 80 and usage["output_tokens"] == 3
    assert usage["billed_micro_usd"] == 28
