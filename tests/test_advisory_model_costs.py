"""Advisory costs retain original accounting while ordinary native work continues."""

import httpx

from companion import ops_status, owner_input
from companion.model import ModelClient
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, worker_for
from tests.support import owner_authority_setup
from tests.support.native_consumer_entry import environment
from tests.support.owner_input import captured
from tests.support.worker_wakeup_process import signed_input


def response(*, usage=True):
    body = {"choices": [{"message": {"role": "assistant", "content": "这是一条合成回复。"},
                         "finish_reason": "stop"}]}
    if usage:
        body["usage"] = {"prompt_tokens": 128, "completion_tokens": 64,
                         "prompt_cache_hit_tokens": 0}
    return httpx.Response(200, json=body)


def configure(core, tmp_path, monkeypatch):
    approved = owner_authority_setup.approved_live

    def approval(core, path):
        return approved(core, path, concurrency=1)

    monkeypatch.setattr(owner_authority_setup, "approved_live", approval)
    return captured(core, tmp_path)


def admit(core, template, key, number):
    capture, message = signed_input(core, template, key, number)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    return core.ingest(message, "advisory-cost-" + str(number))["job_id"]


def test_actual_charges_are_recorded_without_a_money_policy(
    core, tmp_path, monkeypatch,
):
    template, key = configure(core, tmp_path, monkeypatch)
    calls = []

    def provider(request):
        calls.append(request)
        return response()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            core.model = ModelClient(core.settings, http)
            for number in (1, 2):
                job_id = admit(core, template, key, number)
                assert await core.run_once("advisory-cost-worker")
                assert core.store.one("SELECT state FROM jobs WHERE job_id=%s",
                                      (job_id,))["state"] == "SUCCEEDED"

    run(scenario())
    assert len(calls) == 2
    rows = core.store.rows("SELECT state,billed_micro_usd,is_estimate FROM model_usage")
    assert len(rows) == 2 and all(row["state"] == "SETTLED" for row in rows)
    assert all(row["billed_micro_usd"] == 256 for row in rows), rows
    assert core.store.one("SELECT count(*) AS n FROM cloud_cost_snapshots")["n"] == 2


def test_received_unknown_cost_response_and_later_job_continue_with_original_unknown_retained(
    core, tmp_path, monkeypatch,
):
    template, key = configure(core, tmp_path, monkeypatch)
    calls = []
    def provider(request):
        calls.append(request)
        return response(usage=len(calls) != 1)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            core.model = ModelClient(core.settings, http)
            first = admit(core, template, key, 1)
            assert await core.run_once("advisory-cost-worker")
            result = core.store.one("SELECT state,last_error_code FROM jobs WHERE job_id=%s",
                                    (first,))
            assert result["state"] == "SUCCEEDED", result
            original = core.store.one(
                "SELECT request_id,state,billed_micro_usd,is_estimate FROM model_usage "
                "WHERE job_id=%s", (first,))
            assert original["state"] == "UNKNOWN" and original["is_estimate"]
            second = admit(core, template, key, 2)
            assert await core.run_once("advisory-cost-worker")
            assert core.store.one("SELECT state FROM jobs WHERE job_id=%s",
                                  (second,))["state"] == "SUCCEEDED"
            assert core.store.one(
                "SELECT request_id,state,billed_micro_usd,is_estimate FROM model_usage "
                "WHERE request_id=%s", (original["request_id"],)) == original

    run(scenario())
    assert len(calls) == 2
    status = ops_status.snapshot(core.settings)
    assert "MODEL_COST_UNKNOWN" in status["warnings"]
    assert "MODEL_USAGE_UNSETTLED" not in status["blockers"]


def test_missing_response_is_not_fabricated_or_replayed_and_does_not_block_a_fresh_job(
    core, tmp_path, monkeypatch,
):
    template, key = configure(core, tmp_path, monkeypatch)
    calls = []

    def provider(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("synthetic response absent", request=request)
        return response()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            core.model = ModelClient(core.settings, http)
            first = admit(core, template, key, 1)
            assert await core.run_once("advisory-cost-worker")
            assert len(calls) == 1
            assert core.store.one("SELECT state FROM jobs WHERE job_id=%s",
                                  (first,))["state"] != "SUCCEEDED"
            assert not core.store.one(
                "SELECT 1 FROM outbox WHERE origin_event_id="
                "(SELECT event_id FROM jobs WHERE job_id=%s)", (first,))
            original = core.store.one("SELECT request_id,state,billed_micro_usd FROM model_usage "
                                      "WHERE job_id=%s", (first,))
            assert original["state"] == "UNKNOWN"
            second = admit(core, template, key, 2)
            assert await core.run_once("advisory-cost-worker")
            assert core.store.one("SELECT state FROM jobs WHERE job_id=%s",
                                  (second,))["state"] == "SUCCEEDED"
            assert core.store.one("SELECT request_id,state,billed_micro_usd FROM model_usage "
                                  "WHERE request_id=%s", (original["request_id"],)) == original

    run(scenario())
    assert len(calls) == 2


def test_real_http_and_independent_worker_continue_with_original_cost_unknown(
    core, tmp_path,
):
    provider = Provider(response(usage=False).json(), response().json())

    async def scenario():
        async with environment(core, tmp_path, purposes=("chat",), paid_options={
            "concurrency": 1,
        }) as (entry, _), worker_for(core, provider) as worker:
            first = await entry.admit(1, "合成费用未知回复")
            first_id = entry.accepted[first["event_id"]]["job_id"]
            assert await worker.run_once("independent-advisory-worker")
            original = core.store.one("SELECT request_id,state,billed_micro_usd FROM model_usage "
                                      "WHERE job_id=%s", (first_id,))
            assert original["state"] == "UNKNOWN"
            second = await entry.admit(2, "合成后续独立回复")
            second_id = entry.accepted[second["event_id"]]["job_id"]
            assert await worker.run_once("independent-advisory-worker")
            assert core.store.one("SELECT request_id,state,billed_micro_usd FROM model_usage "
                                  "WHERE request_id=%s", (original["request_id"],)) == original
            assert core.store.one("SELECT state FROM jobs WHERE job_id=%s",
                                  (first_id,))["state"] == "SUCCEEDED"
            assert core.store.one("SELECT state FROM jobs WHERE job_id=%s",
                                  (second_id,))["state"] == "SUCCEEDED"
            assert core.store.one("SELECT state,billed_micro_usd FROM model_usage WHERE job_id=%s",
                                  (second_id,)) == {"state": "SETTLED", "billed_micro_usd": 256}

    run(scenario())
    assert len(provider.requests) == 2
