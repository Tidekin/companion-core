"""The native admission ledger owns capacity across processes and SDK continuations."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from companion import budget, capabilities, life_topics, owner_authority, paid_budget
from companion.contracts import CoreError
from companion.model import ModelClient
from companion.store import Store
from tests.owner_model_fixtures import authorized_job
from tests.test_paid_model_gate import approved_live
from tests.test_watch_ledger import ready

MESSAGES = [{"role": "user", "content": "synthetic capacity check"}]


@pytest.mark.parametrize("lane", ["jargon", "style", "watch"])
@pytest.mark.parametrize("boundary", ["admission", "before_io"])
def test_background_capacity_waits_do_not_spend_domain_retries(core, tmp_path, monkeypatch, lane,
                                                              boundary):
    from tests import test_jargon, test_style_distill, test_watch_ledger

    if lane == "jargon":
        for index, text in enumerate(["今天好累", "摸鱼王上线了", "开会", "下班了"], 1):
            test_jargon.say(core, index, text)
        provider = test_jargon.ready(core, tmp_path, test_jargon.EXTRACT)
        step, table = test_jargon.step, "jargon_work"
    elif lane == "style":
        provider = test_style_distill.ready(core, tmp_path, '{"none":true}')
        test_style_distill.thread(core, 1, [
            test_style_distill.comment("1", "猫的睡姿真有意思"),
            test_style_distill.comment("2", "好像按了暂停键一样", parent="1"),
        ])
        step, table = test_style_distill.step, "style_pairs"
    else:
        provider = test_watch_ledger.ready(core, tmp_path, json.dumps(test_watch_ledger.JUDGEMENT))
        test_watch_ledger.observe(core, 1)
        step, table = test_watch_ledger.step, "watch_ledger"

    def full(*args, **kwargs):
        raise CoreError("MODEL_PROVIDER_CONCURRENCY_EXHAUSTED" if boundary == "admission"
                        else "BACKGROUND_YIELD_TO_CHAT", 429)

    with monkeypatch.context() as patch:
        patch.setattr(budget, "reserve" if boundary == "admission" else "_model_scope", full)
        for _ in range(5 if boundary == "admission" else 1):
            assert step(core)
            row = core.store.one(f"SELECT state,attempts FROM {table}")
            assert row == {"state": "RETRY_WAIT", "attempts": 0}
            core.clock.advance(61)
    assert provider.calls == []
    if boundary == "admission":
        assert not core.store.rows("SELECT * FROM model_usage")
    else:
        [usage] = core.store.rows("SELECT request_id,state,billed_micro_usd FROM model_usage")
        assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] == 0
        assert budget.not_dispatched(core.store, usage["request_id"])
    assert step(core)
    assert len(provider.calls) == 1
    assert core.store.one(f"SELECT state FROM {table}")["state"] == {
        "style": "NONE", "watch": "JUDGED", "jargon": "DONE"}[lane]


def approved(core, tmp_path, **changes):
    approved_live(core, tmp_path, concurrency=10, daily_calls=100, window_calls=100,
                  concurrency_basis="local_active_attempts_v2", **changes)
    return authorized_job(core)


@pytest.mark.parametrize("limit", [0, 11, 32, True])
def test_configuration_rejects_concurrency_outside_ten(core, tmp_path, limit):
    with pytest.raises(ValidationError):
        approved_live(core, tmp_path, concurrency=limit)


def test_atomic_admission_across_independent_connections_never_exceeds_ten(core, tmp_path):
    job = approved(core, tmp_path)

    def reserve(index):
        with closing(Store(core.settings.database)) as store:
            try:
                return budget.reserve(store, core.settings, job, "chat", MESSAGES, core.clock(),
                                      clock=core.clock, logical_request_id=f"contender-{index}")
            except CoreError as error:
                assert error.code == "MODEL_PROVIDER_CONCURRENCY_EXHAUSTED"
                return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        admitted = [result for result in pool.map(reserve, range(20)) if result]
    assert len(admitted) == 10
    assert budget.execution_counts(core.store) == {"total": 10, "background": 0}
    assert len(core.store.rows("SELECT * FROM model_usage")) == 10
    with core.store.transaction():
        assert len(paid_budget.journal(core.store, core.settings.paid_model)["model_latest"]) == 10


def test_background_leaves_a_slot_for_chat(core, tmp_path):
    core.settings.watch_ledger = core.settings.watch_ledger.model_copy(
        update={"enabled": True, "daily_micro_usd": 100000})
    job = approved(core, tmp_path)
    # Use the current native policy with its independently granted exploration scope.
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?", (job,))
    view = owner_authority.current(core.store, core.settings, core.clock)
    from tests.test_owner_authority import activate

    active, _ = activate(core, view.policy.model_copy(update={"purposes": ["chat", "exploration"]}),
                         revision=1, operation="grant-background-capacity-check")
    for index in range(9):
        budget.reserve(core.store, core.settings, None, "watch_review", MESSAGES, core.clock(),
                       clock=core.clock, logical_request_id=f"background-{index}")
    with pytest.raises(CoreError, match="MODEL_PROVIDER_CONCURRENCY_EXHAUSTED"):
        budget.reserve(core.store, core.settings, None, "watch_review", MESSAGES, core.clock(),
                       clock=core.clock, logical_request_id="background-overflow")
    # An unbound foreground request cannot borrow owner authority to use the free slot.
    with pytest.raises(CoreError):
        budget.reserve(core.store, core.settings, None, "chat", MESSAGES, core.clock(),
                       clock=core.clock)
    assert active["authority"]["revision"] == 2
    assert not budget.capacity_exhausted(core.store, core.settings.paid_model)
    _, key = core.authorized_model_test_capture
    core.authorized_model_test_capture = active, key
    core.authorized_model_test_jobs = {}
    foreground = authorized_job(core, number=2)
    budget.reserve(core.store, core.settings, foreground, "chat", MESSAGES, core.clock(),
                   clock=core.clock)
    assert budget.execution_counts(core.store) == {"total": 10, "background": 9}


@pytest.mark.parametrize("boundary", ["before_io", "http_hook"])
def test_background_yield_proves_zero_dispatch_without_an_unknown_slot_leak(
    core, tmp_path, monkeypatch, boundary,
):
    provider = ready(core, tmp_path)
    request_id = budget.reserve(core.store, core.settings, None, "watch_review", MESSAGES,
                                core.clock(), clock=core.clock)
    if boundary == "before_io":
        monkeypatch.setattr(owner_authority, "owner_waiting", lambda *a, **kw: True)

    async def yield_in_hook(request):
        monkeypatch.setattr(owner_authority, "owner_waiting", lambda *a, **kw: True)

    async def scenario():
        hooks = {"request": [yield_in_hook]} if boundary == "http_hook" else {}
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider),
                                    event_hooks=hooks) as client:
            with pytest.raises(CoreError, match="BACKGROUND_YIELD_TO_CHAT"):
                await budget.complete(ModelClient(core.settings, client), core.store,
                    core.settings, request_id, MESSAGES, "watch_review", core.clock)

    asyncio.run(scenario())
    assert not provider.calls
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage WHERE request_id=?",
                           (request_id,)) == {"state": "SETTLED", "billed_micro_usd": 0}
    assert budget.execution_counts(core.store)["total"] == 0
    assert core.store.one("SELECT 1 FROM audit_log WHERE action='model.not_dispatched' "
                          "AND entity_id=?", (request_id,))


@pytest.mark.parametrize("transport_unwound", [False, True])
def test_unknown_retains_money_and_blocks_its_input_without_stopping_unrelated_work(
    core, tmp_path, transport_unwound,
):
    job = approved(core, tmp_path)
    request_id = budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock(),
                                clock=core.clock, logical_request_id="uncertain-original")
    before = core.store.one("SELECT billed_micro_usd FROM model_usage WHERE request_id=?",
                            (request_id,))
    if transport_unwound:
        async def scenario():
            def timeout(request):
                raise httpx.ReadTimeout("synthetic lost response", request=request)

            async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
                with pytest.raises(CoreError):
                    await budget.complete(ModelClient(core.settings, client), core.store,
                        core.settings, request_id, MESSAGES, "chat", core.clock)

        asyncio.run(scenario())
    else:
        budget.settle(core.store, core.settings, request_id)
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage WHERE request_id=?",
        (request_id,)) == {"state": "UNKNOWN", **before}
    with core.store.transaction():
        view = paid_budget.journal(core.store, core.settings.paid_model)
    assert (request_id in view["model_closures"]) is transport_unwound
    assert budget.execution_counts(core.store, view["model_closures"])["total"] == (
        0 if transport_unwound else 1)
    assert not life_topics._uncertain(core.store, core.settings)
    assert "MODEL_USAGE_UNKNOWN" not in capabilities._live_blocks(
        core.store, core.settings, core.clock())
    with pytest.raises(CoreError, match="MODEL_OUTCOME_UNKNOWN"):
        budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock(),
                       clock=core.clock, logical_request_id="uncertain-original")
    assert budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock(),
                          clock=core.clock, logical_request_id="independent-input")


def test_tool_roundtrip_waits_for_capacity_then_reuses_saved_response_and_tool(core, tmp_path,
                                                                            monkeypatch):
    job_id = approved(core, tmp_path)
    core.store.execute("UPDATE jobs SET state='QUEUED',lease_token=NULL,attempts=0 WHERE job_id=?",
                       (job_id,))
    original = budget.reserve
    refused = []
    calls = []

    def reserve(*args, **kwargs):
        if len(calls) == 1 and not refused:
            refused.append(True)
            raise CoreError("MODEL_PROVIDER_CONCURRENCY_EXHAUSTED", 429)
        return original(*args, **kwargs)

    monkeypatch.setattr(budget, "reserve", reserve)

    async def provider(request):
        calls.append(json.loads(request.content))
        message = ({"role": "assistant", "content": None, "tool_calls": [{
            "id": "capacity-clock", "type": "function", "function": {
                "name": "current_time", "arguments": "{}"}}]} if len(calls) == 1 else {
                    "role": "assistant", "content": "completed after capacity wait"})
        return httpx.Response(200, json={"choices": [{"message": message}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            core.model = ModelClient(core.settings, client)
            assert await core.run_once("capacity-first", job_id=job_id)
            row = core.store.one("SELECT state,attempts,last_error_code FROM jobs WHERE job_id=?",
                                  (job_id,))
            assert row == {"state": "RETRY_WAIT", "attempts": 0,
                           "last_error_code": "MODEL_PROVIDER_CONCURRENCY_EXHAUSTED"}
            assert not core.store.rows("SELECT * FROM outbox")
            assert core.store.one("SELECT phase FROM agent_conversations")["phase"] == "READY"
            assert budget.chat_pending(core.store, core.clock())
            first_tool = core.store.one("SELECT * FROM agent_tool_calls")
            core.clock.advance(2)
            assert await core.run_once("capacity-resume", job_id=job_id)
            assert core.store.one("SELECT * FROM agent_tool_calls") == first_tool

    asyncio.run(scenario())
    assert len(calls) == 2
    assert any(item["role"] == "tool" and item["tool_call_id"] == "capacity-clock"
               for item in calls[-1]["messages"])
    assert len(core.store.rows("SELECT * FROM model_usage WHERE state='SETTLED'")) == 2
    assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                          (job_id,))["state"] == "SUCCEEDED"
    assert len(core.store.rows("SELECT * FROM outbox WHERE origin_event_id=("
                              "SELECT event_id FROM jobs WHERE job_id=?)", (job_id,))) == 1
    assert Path(core.settings.paid_model.journal_path).exists()
