"""Cost hints do not replace actual response, resource and source contracts."""

import json

import pytest

from companion.model import ModelResponse
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, worker_for
from tests.support.consolidation import enable_nightly
from tests.support.diary import DAY, generate, seed_day
from tests.support.native_consumer_entry import environment


@pytest.mark.parametrize("output_overrun", [False, True])
def test_native_accounting_keeps_the_actual_output_bound(core, tmp_path, output_overrun):
    text = "合成响应的原始内容"
    body = {"choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 128,
                      "completion_tokens": 100000 if output_overrun else 64}}
    provider = Provider(body)

    async def scenario():
        async with environment(core, tmp_path, purposes=("chat",)) as (entry, _), \
            worker_for(core, provider) as worker:
            admitted = await entry.admit(1, "合成资源边界检查")
            job_id = entry.accepted[admitted["event_id"]]["job_id"]
            assert await worker.run_once("native-cost-resource-controls")
            job = core.store.one("SELECT state,last_error_code FROM jobs WHERE job_id=%s",
                                 (job_id,))
            usage = core.store.one("SELECT * FROM model_usage WHERE job_id=%s", (job_id,))
            assert usage["billed_micro_usd"] > 0
            if output_overrun:
                assert job == {"state": "FAILED", "last_error_code": "MODEL_USAGE_UNRESOLVED"}
                assert usage["state"] == "UNKNOWN" and usage["output_tokens"] == 100000
                assert all(json.loads(row["content_json"]).get("text") != text for row in
                           core.store.rows("SELECT content_json FROM outbox"))
            else:
                assert job == {"state": "SUCCEEDED", "last_error_code": None}
                assert usage["state"] == "SETTLED"
            assert not await worker.run_once("no-resource-replay")

    run(scenario())
    assert len(provider.requests) == 1


@pytest.mark.parametrize("scenario", ["diary", "reflection"])
def test_received_background_result_commits_with_original_unknown_cost(core, monkeypatch, scenario):
    model = seed_day(core) if scenario == "diary" else enable_nightly(core)
    original = model.complete

    async def without_usage(messages, kind):
        response = await original(messages, kind)
        return ModelResponse(response.text)

    monkeypatch.setattr(model, "complete", without_usage)
    result = generate(core) if scenario == "diary" else run(core.reflections.run_day(DAY))
    assert result["state"] == "COMMITTED"
    usage = core.store.one("SELECT * FROM model_usage WHERE scenario=%s", (scenario,))
    assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] > 0
    assert len(model.calls) == 1
