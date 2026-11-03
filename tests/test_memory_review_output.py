"""Native SDK memory tools retain source proofs and original paid responsibilities."""

import json

import httpx
import pytest

from companion import budget, owner_authority
from companion.contracts import CoreError
from companion.core import Core
from companion.model import REVIEW_RULES, ModelClient
from companion.store import encode
from tests.conftest import run
from tests.sdk_reader_fixture import answer, call
from tests.support.failure_notice import assert_failure_notice
from tests.support.owner_memory import environment

FACT = "我用🤔表示在考虑青禾项目的蓝色配色"


def tool_response(arguments='{"query":"*"}', *, usage=True):
    value = call("recall-owner-fact", "memory_recall", {})
    value["function"]["arguments"] = arguments
    body = answer(calls=[value])
    if not usage:
        body.pop("usage")
    return httpx.Response(200, json=body)


def tool_result(messages):
    return next(message["content"] for message in messages if message["role"] == "tool")


def assert_settled_calls(core, count, *, scenario="chat"):
    charged = budget.cost(core.settings, 100, 20, scenario=scenario)
    assert charged > 0
    assert core.store.rows(
        "SELECT scenario,state,input_tokens,output_tokens,billed_micro_usd FROM model_usage"
    ) == [{"scenario": scenario, "state": "SETTLED", "input_tokens": 100,
           "output_tokens": 20, "billed_micro_usd": charged}] * count


@pytest.mark.parametrize("arguments", [
    "synthetic non-JSON output", '{}', '{"query":null}', '{"query":42}', '{"query":[]}',
    '{"query":"*","reason":"synthetic extra field"}',
    '```json\n{"query":"*"}\n```', '{"query":""}', encode({"query": "x" * 2001}),
    '{"query":"*","selected":["m0","m0"]}', '{"query":"*","selected":["m999"]}',
], ids=["non-json", "missing", "null", "number", "array", "extra", "fenced", "empty", "long",
        "duplicate-selector", "unknown-selector"])
@pytest.mark.parametrize("prefetched", [False, True], ids=["empty-prefetch", "pinned-prefetch"])
def test_invalid_memory_tool_arguments_recover_within_sdk_budget_without_candidates(
    core, tmp_path, monkeypatch, arguments, prefetched,
):
    async def scenario():
        calls = []

        def provider(request):
            messages = json.loads(request.content)["messages"]
            calls.append(messages)
            if len(calls) == 1:
                return tool_response(arguments)
            assert len(calls) == 2
            assert tool_result(messages)
            assert (FACT in encode(messages)) is prefetched
            return httpx.Response(200, json=answer("在想什么呢？"))

        async with environment(core, tmp_path) as (admit, _, reference):
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    seed, _ = await admit(1, "/记住这个 " + FACT)
                    assert await worker.run_once("seed-owner-fact")
                    assert calls == []
                    before = core.store.rows("SELECT * FROM memories")
                    lookup = worker.memory_context.recall_async
                    queries = []

                    async def prefetch(query, *args, **kwargs):
                        queries.append(query)
                        assert query == "🤔", "Invalid tool arguments reached memory lookup"
                        return await lookup(query, *args, **kwargs) if prefetched else {"rows": []}

                    monkeypatch.setattr(worker.memory_context, "recall_async", prefetch)
                    source, accepted = await admit(2, "🤔")
                    assert await worker.run_once("invalid-memory-arguments")
                    assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                        (accepted["job_id"],)) == {"state": "SUCCEEDED"}
                    assert len(calls) == 2 <= core.settings.paid_model.per_request_calls
                    assert core.store.rows("SELECT * FROM memories") == before
                    assert not core.store.rows("SELECT * FROM agent_tool_calls WHERE job_id=?",
                                               (accepted["job_id"],))
                    assert queries == ["🤔"]
                    consumed = {row["event_id"] for row in core.store.rows(
                        "SELECT event_id FROM job_sources WHERE job_id=?", (accepted["job_id"],))}
                    assert (seed.event_id in consumed) is prefetched
                    assert_settled_calls(core, 2)
                    assert json.loads(core.store.one(
                        "SELECT content_json FROM outbox WHERE origin_event_id=?",
                        (source.event_id,))["content_json"])["text"] == "在想什么呢？"
                    assert not await worker.run_once("no-extra-paid-review")
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as restarted:
                    assert not await restarted.run_once("no-restart-replay")
                assert owner_authority.current(core.store, core.settings, core.clock
                    ).authority.as_dict() == reference
                assert len(calls) == 2
    run(scenario())


def test_repeated_invalid_memory_tool_arguments_stop_at_sdk_retry_limit(core, tmp_path):
    calls = []

    def provider(request):
        calls.append(json.loads(request.content)["messages"])
        assert len(calls) <= 2
        return tool_response('{}')

    async def scenario():
        async with environment(core, tmp_path) as (admit, _, _):
            source, accepted = await admit(1, "🤔")
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    assert await worker.run_once("bounded-sdk-retry")
                    assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                        (accepted["job_id"],)) == {"state": "FAILED"}
                    assert not await worker.run_once("no-job-retry-after-sdk-exhaustion")
            assert len(calls) == 2 <= core.settings.paid_model.per_request_calls
            assert_settled_calls(core, 2)
            assert_failure_notice(core, source.event_id)
            assert not core.store.rows("SELECT * FROM memories")
    run(scenario())


@pytest.mark.parametrize("selection", [None, [], ["m0"]])
def test_emoji_memory_candidates_are_evaluated_in_the_same_sdk_conversation(
    core, tmp_path, selection,
):
    async def scenario():
        calls = []
        reply = "在想什么呢？" if not selection else "你正在考虑青禾项目的蓝色配色。"

        def provider(request):
            messages = json.loads(request.content)["messages"]
            calls.append(messages)
            if len(calls) == 1:
                assert messages[-1]["content"] == "🤔"
                return tool_response()
            result = json.loads(tool_result(messages))
            assert result["state"] == ("NO_RECORD" if selection is None else "FOUND")
            assert [fact["content"] for fact in result["facts"]] == (
                [] if selection is None else [FACT])
            return httpx.Response(200, json=answer(reply))

        async with environment(core, tmp_path) as (admit, _, reference):
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    if selection is not None:
                        await admit(1, "/记住这个 " + FACT)
                        assert await worker.run_once("seed-owner-fact")
                    before = core.store.rows("SELECT * FROM memories")
                    source, accepted = await admit(2, "🤔")
                    assert await worker.run_once("native-memory-relevance")
                    assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                        (accepted["job_id"],)) == {"state": "SUCCEEDED"}
                    assert len(calls) == 2
                    assert_settled_calls(core, 2)
                    assert core.store.rows("SELECT * FROM memories") == before
                    assert json.loads(core.store.one(
                        "SELECT content_json FROM outbox WHERE origin_event_id=?",
                        (source.event_id,))["content_json"])["text"] == reply
                    assert owner_authority.current(core.store, core.settings, core.clock
                        ).authority.as_dict() == reference
    run(scenario())


@pytest.mark.parametrize("prefetched", [False, True], ids=["empty-prefetch", "pinned-prefetch"])
def test_unknown_tool_request_usage_does_not_execute_tool_refund_or_replay(
    core, tmp_path, monkeypatch, prefetched,
):
    async def scenario():
        calls = []

        def provider(request):
            calls.append(json.loads(request.content)["messages"])
            assert len(calls) == 1
            return tool_response(usage=False)

        async with environment(core, tmp_path) as (admit, _, _):
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    seed, _ = await admit(1, "/记住这个 " + FACT)
                    assert await worker.run_once("seed-owner-fact")
                    before = core.store.rows("SELECT * FROM memories")
                    lookup = worker.memory_context.recall_async
                    queries = []

                    async def prefetch(query, *args, **kwargs):
                        queries.append(query)
                        assert query == "🤔", "Unknown provider usage reached tool lookup"
                        return await lookup(query, *args, **kwargs) if prefetched else {"rows": []}

                    monkeypatch.setattr(worker.memory_context, "recall_async", prefetch)
                    source, accepted = await admit(2, "🤔")
                    assert await worker.run_once("unknown-tool-accounting")
                    assert core.store.one("SELECT state,last_error_code FROM jobs WHERE job_id=?",
                        (accepted["job_id"],)) == {
                            "state": "FAILED", "last_error_code": "MODEL_OUTCOME_UNKNOWN"}
                    usage = core.store.one("SELECT u.state,u.billed_micro_usd,a.reserved_micro_usd "
                        "FROM model_usage u JOIN model_admissions a USING(request_id)")
                    assert usage["state"] == "UNKNOWN"
                    assert usage["billed_micro_usd"] == usage["reserved_micro_usd"] > 0
                    assert queries == ["🤔"]
                    consumed = {row["event_id"] for row in core.store.rows(
                        "SELECT event_id FROM job_sources WHERE job_id=?", (accepted["job_id"],))}
                    assert (seed.event_id in consumed) is prefetched
                    assert core.store.rows("SELECT * FROM memories") == before
                    assert not core.store.rows("SELECT * FROM outbox WHERE origin_event_id=?",
                                               (source.event_id,))
                    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
                        owner_authority.current(core.store, core.settings, core.clock)
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as restarted:
                    assert not await restarted.run_once("no-unknown-replay")
                assert len(calls) == 1
    run(scenario())


@pytest.mark.parametrize("interruption", ["recall", "supersede"])
def test_memory_tool_result_cannot_override_recall_or_supersession(core, tmp_path, interruption):
    async def scenario():
        calls = []

        async def provider(request):
            messages = json.loads(request.content)["messages"]
            calls.append(messages)
            if len(calls) == 1:
                return tool_response()
            assert FACT in tool_result(messages)
            assert seed.event_id in {row["event_id"] for row in core.store.rows(
                "SELECT event_id FROM job_sources WHERE job_id=?", (accepted["job_id"],))}
            if interruption == "recall":
                recalled = await recall(seed)
                assert recalled.status_code == 200, recalled.text
            else:
                await admit(3, "现在换个话题")
            return httpx.Response(200, json=answer("这个旧答案不得发送。"))

        async with environment(core, tmp_path) as (admit, recall, reference):
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    seed, _ = await admit(1, "/记住这个 " + FACT)
                    assert await worker.run_once("seed-owner-fact")
                    source, accepted = await admit(2, "🤔")
                    assert await worker.run_once("interrupted-memory-answer")
                    assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                        (accepted["job_id"],)) == {"state": "CANCELLED"}
                    assert len(calls) == 2
                    assert_settled_calls(core, 2)
                    assert not core.store.rows("SELECT * FROM outbox WHERE origin_event_id=?",
                                               (source.event_id,))
                    assert owner_authority.current(core.store, core.settings, core.clock
                        ).authority.as_dict() == reference
    run(scenario())


def test_invalid_memory_write_review_rejects_new_fact_with_bounded_paid_retries(core, tmp_path):
    async def scenario():
        calls = []

        def provider(request):
            messages = json.loads(request.content)["messages"]
            calls.append(messages)
            assert messages[0]["content"] == REVIEW_RULES
            return httpx.Response(200, json=answer("synthetic non-JSON output"))

        async with environment(core, tmp_path) as (admit, _, _):
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    await admit(1, "/记住这个 " + FACT)
                    assert await worker.run_once("seed-owner-fact")
                    before = core.store.rows("SELECT * FROM memories")
                    source, accepted = await admit(2, "/记住这个 " + FACT.replace("蓝色", "红色"))
                    for attempt in range(core.settings.max_attempts):
                        assert await worker.run_once("invalid-write-review")
                        final = attempt + 1 == core.settings.max_attempts
                        assert core.store.one(
                            "SELECT state,last_error_code FROM jobs WHERE job_id=?",
                            (accepted["job_id"],)) == {
                                "state": "FAILED" if final else "RETRY_WAIT",
                                "last_error_code": "MEMORY_REVIEW_INVALID"}
                        assert core.store.rows("SELECT * FROM memories") == before
                        assert not core.store.rows("SELECT 1 FROM memory_evidence WHERE event_id=?",
                                                   (source.event_id,))
                        core.clock.advance(60)
                    assert len(calls) == core.settings.max_attempts
                    assert_settled_calls(core, len(calls), scenario="memory_review")
                    assert not await worker.run_once("no-extra-write-retry")
    run(scenario())
