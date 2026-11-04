"""Inference yields; the original SQLite/source fences stay on the calling thread."""

import asyncio
import threading

import numpy as np
import pytest

from companion import reply_revision
from companion.contracts import CoreError
from tests.conftest import ingest, run
from tests.test_memory_commands import fact


@pytest.mark.parametrize(
    "change", ["none", "forget", "correct", "source", "source_edit", "expiry", "input", "cancel"],
)
def test_async_recall_yields_and_rejects_changes_before_vector_commit(core, monkeypatch, change):
    mid = fact(core, 1, "青禾项目使用蓝色配色")
    ingest(core, 2, "青禾项目使用什么配色")
    job = core.claim_job("recall")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    owner_thread = threading.get_ident()
    writes = []
    execute = core.store.execute

    def record(sql, params=()):
        if "INSERT OR REPLACE INTO memory_vectors" in sql:
            writes.append(threading.get_ident())
        assert threading.get_ident() == owner_thread
        return execute(sql, params)

    def infer(query, contents):
        assert threading.get_ident() != owner_thread
        entered.set()
        try:
            assert release.wait(5)
            return tuple(np.array([1., 0.]) for _ in contents), np.array([1., 0.])
        finally:
            finished.set()

    monkeypatch.setattr(core.store, "execute", record)
    monkeypatch.setattr(core.memory.index, "_infer", infer)

    async def scenario():
        task = asyncio.create_task(core.recall_for_reply(job))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            heartbeats = 0
            for _ in range(3):
                await asyncio.sleep(.005)
                heartbeats += 1
            assert heartbeats == 3 and not task.done()
            if change == "forget":
                core.memory.decide(mid, "forget", core.clock())
            elif change == "correct":
                core.memory.decide(mid, "correct", core.clock(), "青禾项目使用绿色配色")
            elif change == "source":
                with core.store.transaction():
                    core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
            elif change == "source_edit":
                core.store.execute("UPDATE events SET body_json=? WHERE event_id='evt_test_1'",
                                   ('{"text":"来源已变更"}',))
            elif change == "expiry":
                core.clock.advance(core.settings.raw_event_retention_days * 86400 + 1)
            elif change == "input":
                ingest(core, 3, "先回答新的问题")
                # The offline fixture has no private window; exercise its native revision gate.
                with core.store.transaction():
                    reply_revision.advance(core.store, job["conversation_id"], "evt_test_3",
                                           core.clock())
            elif change == "cancel":
                task.cancel()
            release.set()
            if change == "none":
                rows, paused = await task
                assert [r["memory_id"] for r in rows] == [mid] and not paused
                assert writes == [owner_thread]
            else:
                with pytest.raises(asyncio.CancelledError if change == "cancel" else CoreError):
                    await task
                assert not writes
                assert not core.store.rows("SELECT * FROM memory_vectors")
                assert not core.model.calls
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            assert await asyncio.to_thread(finished.wait, 3)

    run(scenario())


def test_sync_and_async_share_ranking_and_evidence(core, monkeypatch):
    mid = fact(core, 1, "青禾项目使用蓝色配色")
    monkeypatch.setattr(core.memory.index, "_infer", lambda query, texts: (
        tuple(np.array([1., 0.]) for _ in texts), np.array([1., 0.]),
    ))
    sync_trace, async_trace, sync_explain, async_explain = {}, {}, {}, {}
    synchronous = core.memory.search("青禾项目", now=core.clock(),
                                     trace=sync_trace, explain=sync_explain)
    asynchronous = run(core.memory.search_async("青禾项目", now=core.clock(), clock=core.clock,
                                               trace=async_trace, explain=async_explain))
    assert synchronous == asynchronous
    assert sync_trace == async_trace and sync_explain == async_explain == sync_trace["selected"]
    assert synchronous[0]["memory_id"] == mid
    assert synchronous[0]["evidence_ids"] == ["evt_test_1"]


def test_context_trace_only_retains_final_sources_and_marks_project_fallback(core, monkeypatch):
    ids = {fact(core, i, f"青禾项目采用第{i}号方案") for i in range(1, 4)}
    core.store.execute("UPDATE memories SET needs_review=1")
    monkeypatch.setattr(core.memory.index, "_infer", lambda query, texts: (
        tuple(np.array([1., 0.]) for _ in texts), np.array([1., 0.]),
    ))
    trace = {}
    result = run(core.memory_context.recall_async("青禾项目", core.clock(), trace=trace))
    final_ids = {row["memory_id"] for row in result["rows"]}
    assert len(final_ids) == 2 and final_ids < ids
    assert set(trace["selected"]) == final_ids and trace["conflicts_truncated"]
    assert not any(mid in str(trace) for mid in ids - final_ids)

    core.store.execute("UPDATE memories SET needs_review=0")
    monkeypatch.setattr(core.memory.index, "_infer", lambda query, texts: (
        tuple(np.array([1., 0.]) for _ in texts), np.array([0., 1.]),
    ))
    trace = {}
    result = run(core.memory_context.recall_async("我让你记住的项目事实是什么？", core.clock(),
                                                project_query="项目上你记得什么？", trace=trace))
    assert {row["memory_id"] for row in result["rows"]} == ids
    assert trace["search_selected_count"] == 0
    assert trace["selected"] == {mid: {"project_fallback": True} for mid in ids}


def test_cancelled_inference_keeps_cpu_slot_until_thread_finishes(core, monkeypatch):
    fact(core, 1, "青禾项目使用蓝色配色")
    entered, release = threading.Event(), threading.Event()
    queries = []

    class Model:
        def passage_embed(self, texts, batch_size):
            return (np.array([1., 0.]) for _ in texts)

        def query_embed(self, query):
            queries.append(query)
            entered.set()
            assert release.wait(5)
            return iter([np.array([1., 0.])])

    monkeypatch.setattr(core.memory.index, "warmup", Model)

    async def scenario():
        first = asyncio.create_task(core.memory.search_async("第一次", now=core.clock()))
        second = None
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(core.memory.search_async("第二次", now=core.clock()))
            await asyncio.sleep(.03)
            assert queries == ["第一次"]
            assert not core.store.rows("SELECT * FROM memory_vectors")
            release.set()
            assert await second
            assert queries == ["第一次", "第二次"]
        finally:
            release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)

    run(scenario())
