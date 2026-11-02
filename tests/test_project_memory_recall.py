"""Project recall can nominate confirmed facts without bypassing relevance review."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from companion.store import encode
from tests.conftest import ingest, run
from tests.sdk_reader_fixture import answer, call
from tests.support.failure_notice import assert_failure_notice
from tests.support.native_recall_entry import native_recall, returned
from tests.test_live_image_input import options as image_options
from tests.test_memory_commands import fact
from tests.test_memory_review_output import assert_settled_calls


def empty_semantic_search(core, monkeypatch):
    core.settings.max_recent_messages = 0
    # Reproduce the live failure independently of embedding model numeric drift.
    monkeypatch.setattr(core.memory.index, "scores",
                        lambda query, rows: {row["memory_id"]: 0.1 for row in rows})


@pytest.mark.parametrize("query", [
    "你还记得我之前明确确认过的那条项目事实吗？请说说。",
    "之前确认的项目设定是什么？",
    "我让你记住的项目事实是什么？",
    "项目上你记得什么？",
    "请回顾一下我们以前的项目约定。",
    "你之前记下的项目信息有哪些？",
    "你还记得之前确认的项目事实吗？不要猜测。",
    "请回顾之前确认的项目约定，不用展开。",
    "你记得项目事实吗？不要使用列表。",
    "What do you remember about our project?",
    "Recall the previously confirmed project settings.",
])
def test_generic_project_recall_reaches_review_when_semantic_search_is_empty(
    core, monkeypatch, query,
):
    fact(core, 1, "这个工程使用琥珀作为内部验收代号。")
    empty_semantic_search(core, monkeypatch)
    ingest(core, 2, query)
    assert run(core.run_once())
    assert [scenario for _, scenario in core.model.calls] == ["memory_review", "chat"]
    assert "琥珀" in core.model.calls[-1][0][1]["content"]


@pytest.mark.parametrize("query", [
    "不要再回忆之前确认的项目事实。",
    "不谈记忆，项目今晚能不能上线？",
    "别提之前的项目约定，聊聊天气。",
    "之前没确认的项目设定是什么？",
    "项目里的记忆缓存如何设计？",
    "项目之前已确认上线了，今天天气怎样？",
    "我还记得项目约定。今天吃什么？",
    "项目已经确认了。你记得我喜欢喝什么吗？",
    "你记得我喜欢喝什么吗？",
    "今天下午会下雨吗？",
    "Don't recall the project facts.",
    "I remember our project. What is the weather today?",
])
def test_negative_or_unrelated_queries_do_not_add_a_review(core, monkeypatch, query):
    fact(core, 1, "这个工程使用琥珀作为内部验收代号。")
    empty_semantic_search(core, monkeypatch)
    ingest(core, 2, query)
    assert run(core.run_once())
    assert [scenario for _, scenario in core.model.calls] == ["chat"]
    assert "琥珀" not in encode(core.model.calls)


@pytest.mark.parametrize("ineligible", [
    "hypothesis", "conflict", "missing_evidence", "redacted_evidence", "other_subject",
])
def test_fallback_uses_existing_evidence_and_only_confirmed_project_facts(
    core, monkeypatch, ineligible,
):
    memory_id = fact(core, 1, "这个工程使用琥珀作为内部验收代号。")
    if ineligible == "hypothesis":
        core.store.execute("UPDATE memories SET status='HYPOTHESIS' WHERE memory_id=?",
                           (memory_id,))
    elif ineligible == "conflict":
        core.store.execute("UPDATE memories SET needs_review=1 WHERE memory_id=?", (memory_id,))
    elif ineligible == "missing_evidence":
        core.store.execute("DELETE FROM memory_evidence WHERE memory_id=?", (memory_id,))
    elif ineligible == "redacted_evidence":
        core.store.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_1'")
    else:
        core.store.execute("UPDATE memories SET subject='user' WHERE memory_id=?", (memory_id,))
    empty_semantic_search(core, monkeypatch)
    ingest(core, 2, "项目上你记得什么？")
    assert run(core.run_once())
    assert [scenario for _, scenario in core.model.calls] == ["chat"]
    assert "琥珀" not in encode(core.model.calls)


def test_ordinary_catalog_is_bounded_and_selector_can_reject_every_candidate(core, monkeypatch):
    for number in range(8):
        fact(core, number, f"这个工程的第{number}项确认代号是琥珀{number}。")
    empty_semantic_search(core, monkeypatch)
    core.model.recall = lambda payload: '{"relevant":[]}'
    ingest(core, 100, "项目上你记得什么？")
    assert run(core.run_once())
    assert [scenario for _, scenario in core.model.calls] == ["memory_review", "chat"]
    payload = json.loads(core.model.calls[0][0][-1]["content"])
    assert len(payload["memories"]) == 6
    assert all(row["subject"] == "project" and row["status"] == "CONFIRMED"
               for row in payload["memories"])
    assert "琥珀" not in encode(core.model.calls[-1][0])


async def low_native_scores(query, rows, *, revalidate):
    revalidate()
    return {row["memory_id"]: 0.1 for row in rows}


@pytest.mark.parametrize("select", [True, False])
def test_native_project_fallback_keeps_tool_proofs_and_same_answer_review(
    core, tmp_path, monkeypatch, select,
):
    def response(request, payload, number):
        body = answer("已核对：青竹七号。" if select else "当前问题不需要使用这条事实。")
        if not returned(payload):
            body = answer(calls=[call("project-fallback", "memory_recall",
                                     {"query": "我让你记住的项目事实是什么？"})])
        return httpx.Response(200, json=body)

    async def scenario():
        async with native_recall(core, tmp_path, query="我让你记住的项目事实是什么？",
                                 respond=response) as value:
            # Exercise the original project fallback inside the actual memory tool.
            scores = AsyncMock(side_effect=low_native_scores)
            monkeypatch.setattr(value.worker.memory.index, "scores_async", scores)
            source = await value.capture()
            assert await value.worker.run_once("native-project-fallback")
            assert len(value.calls) == 2 and "紫云三号" not in encode(value.calls)
            assert scores.await_count == 2  # Initial prefetch and the explicit model tool.
            result = json.loads(returned(value.calls[-1])[0]["content"])
            assert [row["memory_id"] for row in result["facts"]] == [value.memory_id]
            assert result["requires_relevance_review"]
            # The same SDK answer assesses relevance; every consumed tool source stays pinned.
            assert value.source["event_id"] in {row["event_id"] for row in core.store.rows(
                "SELECT event_id FROM job_sources WHERE job_id=(SELECT job_id FROM jobs "
                "WHERE event_id=?)", (source["event_id"],))}
            outbox = core.store.one("SELECT content_json FROM outbox WHERE origin_event_id=?",
                                    (source["event_id"],))
            assert ("青竹七号" in outbox["content_json"]) is select
            assert_settled_calls(core, 2)
            assert core.store.one("SELECT status,confidence FROM memories WHERE memory_id=?",
                                  (value.outsider,)) == {"status": "CONFIRMED", "confidence": 1.0}
    run(scenario())



def test_live_fallback_still_consumes_the_paid_call_budget(core, tmp_path, monkeypatch):
    async def scenario():
        async with native_recall(core, tmp_path, query="之前确认的项目设定是什么？",
                                 paid={"window_calls": 1, "daily_calls": 1}) as value:
            scores = AsyncMock(side_effect=low_native_scores)
            monkeypatch.setattr(value.worker.memory.index, "scores_async", scores)
            await value.capture()
            assert await value.worker.run_once("native-project-budget")
            assert len(value.calls) == 1 and not value.sends
            assert scores.await_count == 2
            assert_settled_calls(core, 1)
            assert_failure_notice(core)
            assert core.store.one("SELECT COUNT(*) n FROM model_admissions")["n"] == 1
    run(scenario())



@pytest.mark.parametrize("input_limit", [16000, 1000])
def test_selected_fact_reaches_chat_with_deployed_rich_content_configuration(
    core, tmp_path, monkeypatch, input_limit,
):
    image = image_options()

    async def scenario():
        async with native_recall(core, tmp_path,
            query="你还记得我之前明确确认过的那条项目事实吗？请说说。",
            fact_text="本次验收项目代号是青竹七号", additional_purposes=("image", "content"),
            core_options={**image["core_options"], "context_soft_tokens": 3200,
                          "max_output_tokens": 768},
            paid={**image["paid_options"], "per_request_calls": 3,
                  "max_input_tokens": input_limit, "max_output_tokens": 768}) as value:
            scores = AsyncMock(side_effect=low_native_scores)
            monkeypatch.setattr(value.worker.memory.index, "scores_async", scores)
            assert len(value.source["event_id"]) == 68
            source = await value.capture()
            assert await value.worker.run_once("rich-native-project-context")
            if input_limit == 16000:
                assert len(value.calls) == 2
                assert scores.await_count == 2
                result = json.loads(returned(value.calls[-1])[0]["content"])
                assert [row["memory_id"] for row in result["facts"]] == [value.memory_id]
                assert "青竹七号" in encode(value.calls[-1]["messages"])
                assert_settled_calls(core, 2)
            else:
                # The native tool catalog is part of the actual first request bound.
                assert len(value.calls) <= 1 and not value.sends
                assert_failure_notice(core)
                assert core.store.one("SELECT last_error_code FROM jobs WHERE event_id=?",
                    (source["event_id"],))["last_error_code"] == "MODEL_INPUT_LIMIT"
            for row in core.store.rows("SELECT input_bound FROM model_admissions"):
                assert row["input_bound"] <= input_limit
    run(scenario())
