"""Memory integration through authenticated HTTP and independently opened Core workers."""

import asyncio
import json

import httpx
import pytest

from companion import owner_authority
from companion.core import Core
from companion.model import EXTRACT_RULES, ModelClient
from companion.store import after, encode
from tests.conftest import ingest, run
from tests.support.core_http import real_core_http
from tests.support.owner_memory import environment as owner_memory_environment
from tests.test_next_life_growth import feedback_days

FACT = "青禾项目下一步检查配色"


def response(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text},
                                                "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


def recall_request(query):
    return httpx.Response(200, json={"choices": [{"message": {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "recall-fact", "type": "function", "function": {
                "name": "memory_recall", "arguments": encode({"query": query})}}]},
        "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


def test_authorized_history_can_exceed_individual_memory_search_query_limit(
    core, tmp_path,
):
    async def scenario():
        prompts = []

        def provider(request):
            prompts.append(json.loads(request.content)["messages"])
            return response("合成回复")

        async with owner_memory_environment(core, tmp_path) as (admit, recall, reference):
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    texts = [f"设计记录第{number}段：" + "线条" * 190 for number in range(1, 4)]
                    assert sum(map(len, texts)) > 1000
                    for number, text in enumerate(texts, 1):
                        _, accepted = await admit(number, text)
                    # Earlier unanswered inputs remain authorized context after supersession.
                    assert await worker.run_once("long-history-chat")
                    assert worker.store.one("SELECT state FROM jobs WHERE job_id=?",
                                            (accepted["job_id"],))["state"] == "SUCCEEDED"
                    assert len(prompts) == 1
                    assert all(text in encode(prompts[-1]) for text in texts)
                    assert owner_authority.current(core.store, core.settings, core.clock
                        ).authority.as_dict() == reference
    run(scenario())


@pytest.mark.parametrize("stage", ["before-dispatch", "during-response"])
def test_every_fact_source_is_tracked_before_await_and_recall_keeps_independent_fact(
    core, tmp_path, stage,
):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def before_dispatch(request):
            messages = json.loads(request.content)["messages"]
            if stage == "before-dispatch" and any(m["role"] == "tool" for m in messages):
                entered.set()
                await release.wait()

        async def provider(request):
            messages = json.loads(request.content)["messages"]
            kind = "chat" if any(m["role"] == "tool" for m in messages) else "recall-request"
            calls.append(kind)
            if kind == "recall-request":
                return recall_request("青禾项目")
            assert FACT in encode(messages)
            if stage == "during-response":
                entered.set()
                await release.wait()
            return response("合成回答")

        async with owner_memory_environment(core, tmp_path) as (admit, recall, reference):
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider),
                event_hooks={"request": [before_dispatch]}) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    assert worker.store is not core.store
                    sources, captures = [], []
                    for number in range(1, 10):
                        source, _ = await admit(number, "/记住这个 " + FACT)
                        assert await worker.run_once("seed-control")
                        sources.append(source.event_id)
                        captures.append(source)
                    assert calls == []
                    projected = worker.memory_context.recall("*", worker.clock())["facts"][0]
                    assert len(projected["evidence"]) == 8 and projected["evidence_truncated"]
                    assert set(projected["evidence_ids"]) == set(sources)
                    assert sources[0] not in {row["event_id"] for row in projected["evidence"]}
                    question, accepted = await admit(
                        10, "青禾项目下一步做什么？",
                    )
                    task = asyncio.create_task(worker.run_once("actual-memory-reply"))
                    try:
                        await asyncio.wait_for(entered.wait(), 10)
                        tracked = {row["event_id"] for row in core.store.rows(
                            "SELECT event_id FROM job_sources WHERE job_id=?",
                            (accepted["job_id"],),
                        )}
                        assert set(sources) | {question.event_id} <= tracked
                        recalled = await recall(captures[0])
                        assert recalled.status_code == 200, recalled.text
                        release.set()
                        assert await asyncio.wait_for(task, 10)
                    finally:
                        release.set()
                        await asyncio.gather(task, return_exceptions=True)
                    assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                                          (accepted["job_id"],))["state"] == "CANCELLED"
                    assert not core.store.rows("SELECT * FROM outbox WHERE origin_event_id=?",
                                               (question.event_id,))
                    assert core.store.one("SELECT redacted FROM events WHERE event_id=?",
                                          (question.event_id,))["redacted"] == 0
                    assert not core.store.rows("SELECT * FROM event_dependencies WHERE event_id=?",
                                               (question.event_id,))
                    assert core.store.one("SELECT status FROM memories")["status"] == "CONFIRMED"
                    assert core.store.one("SELECT COUNT(*) n FROM memory_evidence")["n"] == 8
                    charged = core.store.rows(
                        "SELECT state FROM model_usage WHERE billed_micro_usd>0")
                    assert charged == [{"state": "SETTLED"} for _ in calls]
                    assert calls == (["recall-request"] if stage == "before-dispatch"
                                     else ["recall-request", "chat"])
                    assert owner_authority.current(core.store, core.settings, core.clock
                        ).authority.as_dict() == reference
    run(scenario())


def test_continuation_uses_full_episode_proof_including_undisplayed_sources(
    core, tmp_path,
):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        hold = False
        captured = []

        async def provider(request):
            messages = json.loads(request.content)["messages"]
            if messages[0]["content"] == EXTRACT_RULES:
                return response('{"memories":[]}')
            if hold:
                captured.append(messages)
                entered.set()
                await release.wait()
            return response("合成回复")

        async with owner_memory_environment(core, tmp_path) as (
            admit_input, recall_input, reference,
        ):
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    sources, captures = [], []
                    for number in range(1, 6):
                        source, _ = await admit_input(number, f"设计进度第{number}步")
                        assert await worker.run_once("historical-chat")
                        sources.append(source.event_id)
                        captures.append(source)
                    assert await worker.extract_once()
                    assert core.store.one("SELECT COUNT(*) n FROM episodes")["n"] == 1
                    question, accepted = await admit_input(6, "继续上次的设计")
                    hold = True
                    task = asyncio.create_task(worker.run_once("continuation-chat"))
                    try:
                        await asyncio.wait_for(entered.wait(), 10)
                        message = next(m for m in captured[0]
                                       if isinstance(m.get("content"), str)
                                       and m["content"].startswith("相关情景接续"))
                        items = json.loads(message["content"].split("\n", 1)[1])
                        assert set(items[0]["source_event_ids"]) == set(sources)
                        assert sources[0] not in {row["event_id"] for row in items[0]["excerpts"]}
                        tracked = {row["event_id"] for row in core.store.rows(
                            "SELECT event_id FROM job_sources WHERE job_id=?",
                            (accepted["job_id"],),
                        )}
                        assert set(sources) <= tracked
                        recalled = await recall_input(captures[0])
                        assert recalled.status_code == 200, recalled.text
                        release.set()
                        assert await asyncio.wait_for(task, 10)
                    finally:
                        release.set()
                        await asyncio.gather(task, return_exceptions=True)
                    assert not core.store.rows("SELECT * FROM outbox WHERE origin_event_id=?",
                                               (question.event_id,))
                    current = owner_authority.current(core.store, core.settings, core.clock)
                    assert current.authority.as_dict() == reference
    run(scenario())


def test_expired_fact_is_absent_from_real_http_search_and_independent_worker(core, tmp_path):
    calls = []

    def provider(request):
        calls.append(json.loads(request.content)["messages"])
        assert FACT not in encode(calls[-1])
        return response("当前没有可用记录")

    async def work():
        async with owner_memory_environment(core, tmp_path, history=False) as (admit, _, _):
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    assert worker.store is not core.store
                    await admit(1, "/记住这个 " + FACT)
                    assert await worker.run_once("seed-valid-fact")
                    assert calls == []
                    mid = core.store.one("SELECT memory_id FROM memories")["memory_id"]
                    core.store.execute("UPDATE memories SET valid_to=? WHERE memory_id=?",
                                       (after(core.clock(), -1), mid))
                    async with real_core_http(worker) as port, httpx.AsyncClient(
                        base_url=f"http://127.0.0.1:{port}", trust_env=False,
                        headers={"Authorization": "Bearer "
                                 + core.settings.bridge_token.get_secret_value()},
                    ) as client:
                        result = await client.post("/internal/v1/memories/search",
                            json={"query": "*", "requester_actor_id": "owner"})
                        assert result.status_code == 200, result.text
                        assert result.json() == {"items": []}
                    await admit(2, "这个项目还有记录吗？")
                    assert await worker.run_once()
    run(work())
    assert len(calls) == 1


def test_fact_validity_is_rechecked_after_model_await_without_refunding_usage(core, tmp_path):
    calls = []

    def provider(request):
        calls.append(json.loads(request.content)["messages"])
        if not any(message["role"] == "tool" for message in calls[-1]):
            return recall_request("青禾项目")
        assert FACT in encode(calls[-1])
        core.clock.advance(2)
        return response("合成回答")

    async def work():
        async with owner_memory_environment(core, tmp_path, history=False) as (admit, _, _):
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    await admit(1, "/记住这个 " + FACT)
                    assert await worker.run_once("seed-valid-fact")
                    assert calls == []
                    mid = core.store.one("SELECT memory_id FROM memories")["memory_id"]
                    question, accepted = await admit(2, "青禾项目下一步做什么？")
                    core.store.execute("UPDATE memories SET valid_to=? WHERE memory_id=?",
                                       (after(core.clock(), 1), mid))
                    assert await worker.run_once()
                    assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                        (accepted["job_id"],)) == {"state": "CANCELLED"}
                    assert not core.store.rows("SELECT * FROM outbox WHERE origin_event_id=?",
                                               (question.event_id,))
    run(work())
    assert len(calls) == 2
    assert core.store.rows("SELECT state FROM model_usage") == [{"state": "SETTLED"}] * 2


@pytest.mark.parametrize("mode", ["enabled", "disabled", "transient"])
def test_expression_hook_uses_actual_owner_sources_and_short_feedback_wins(
    core, tmp_path, mode,
):
    core.settings.expression_learning_enabled = mode != "disabled"
    core.settings.expression_enabled = True
    core.settings.affect_enabled = True
    calls = []

    def provider(request):
        calls.append(json.loads(request.content)["messages"])
        return response("合成回复")

    async def scenario():
        async with owner_memory_environment(core, tmp_path, history=False,
            additional_purposes=("affect", "expression")) as (admit, _, reference):
            historical = set()
            for number in range(1, 8):
                source, _ = await admit(number, "简短一点")
                historical.add(source.event_id)
                core.clock.advance(86400)
            core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
            source, accepted = await admit(100,
                                           "简短一点" if mode == "transient" else "今天聊点什么")
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    assert await worker.run_once()
            prompt = encode(calls[-1])
            assert ("主人长期表达偏好参考" in prompt) is (mode == "enabled")
            if mode == "transient":
                assert "当前交互参考" in prompt
            tracked = {row["event_id"] for row in core.store.rows(
                "SELECT event_id FROM job_sources WHERE job_id=?", (accepted["job_id"],),
            )}
            assert source.event_id in tracked
            assert (historical <= tracked) is (mode == "enabled")
            assert owner_authority.current(core.store, core.settings, core.clock
                        ).authority.as_dict() == reference
    run(scenario())


def test_history_is_explicit_and_current_recall_does_not_reintroduce_revision_text(
    core, tmp_path,
):
    old, new = "项目配色为青色", "项目配色为紫色"
    calls = []

    def provider(request):
        messages = json.loads(request.content)["messages"]
        calls.append(messages)
        assert old not in encode(messages)
        if not any(message["role"] == "tool" for message in messages):
            return recall_request("项目配色")
        assert new in encode(messages)
        return response("现在是紫色")

    async def scenario():
        async with owner_memory_environment(core, tmp_path) as (admit, recall, reference):
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    await admit(1, "/记住这个 " + old)
                    assert await worker.run_once()
                    mid = core.store.one("SELECT memory_id FROM memories")["memory_id"]
                    await admit(2, f"/纠正这一条 {mid} {new}")
                    assert await worker.run_once()
                    revised = core.store.one("SELECT memory_id FROM memories WHERE status="
                                             "'CONFIRMED'")["memory_id"]
                    source, _ = await admit(3,
                                            f"/你记得什么 历史 {revised}")
                    assert await worker.run_once()
                    reply = core.store.one(
                        "SELECT content_json FROM outbox WHERE origin_event_id=?",
                        (source.event_id,),
                    )["content_json"]
                    assert old in reply and new in reply and "SUPERSEDED" in reply
                    assert not calls
                    await admit(4, "项目配色是什么？")
                    assert await worker.run_once()
                    assert len(calls) == 2
    run(scenario())


def test_strict_fact_scope_does_not_authorize_episode_context(core, tmp_path):
    canary = "未授予历史读取用途的情景原文"
    calls = []

    def provider(request):
        messages = json.loads(request.content)["messages"]
        calls.append(messages)
        if messages[-1]["role"] == "user" and messages[-1]["content"] == "配色":
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "recall-color", "type": "function", "function": {
                        "name": "memory_recall", "arguments": '{"query":"配色"}'}}]},
                "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
        return response("合成回复")

    async def scenario():
        async with owner_memory_environment(core, tmp_path, history=False) as (admit_input, _, _):
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                with Core(core.settings, ModelClient(core.settings, transport),
                          core.clock) as worker:
                    source, _ = await admit_input(80, canary)
                    assert await worker.run_once()
                    worker.memory.episodes(worker.store.rows(
                        "SELECT * FROM events WHERE event_id=?", (source.event_id,)),
                        worker.clock())
                    await admit_input(81, "/记住这个 " + FACT)
                    assert await worker.run_once()
                    assert worker.store.one("SELECT content FROM memories") == {"content": FACT}
                    calls.clear()
                    _, accepted = await admit_input(82, "配色")
                    assert await worker.run_once()
                    assert len(calls) == 2, worker.store.rows(
                        "SELECT * FROM audit_log WHERE action LIKE 'memory.%'")
                    assert FACT in encode(calls)
                    assert "相关情景接续" not in encode(calls) and canary not in encode(calls)
                    assert source.event_id not in {row["event_id"] for row in worker.store.rows(
                        "SELECT event_id FROM job_sources WHERE job_id=?", (accepted["job_id"],))}
    run(scenario())


def test_dropped_expression_reference_does_not_register_its_sources(core):
    core.settings.expression_learning_enabled = True
    core.settings.expression_enabled = True
    feedback_days(core)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    historical = {row["event_id"] for row in core.store.rows("SELECT event_id FROM events")}
    ingest(core, 100, "今天聊点什么")
    job = core.claim_job("context-fit")
    core.settings.context_soft_tokens = 1
    snapshots = []
    messages = core.context(job, snapshots=snapshots)
    assert "主人长期表达偏好参考" not in encode(messages)
    assert not snapshots
    assert not historical & {row["event_id"] for row in core.store.rows(
        "SELECT event_id FROM job_sources WHERE job_id=?", (job["job_id"],),
    )}
