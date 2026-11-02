"""Real HTTP plus an independent native worker; inference and providers are controlled."""

import asyncio
import json
import threading

import httpx
import numpy as np
import pytest

from companion.contracts import Candidate
from companion.core import Core
from companion.model import ModelClient
from companion.retrieval import SemanticIndex
from tests.conftest import run
from tests.sdk_reader_fixture import answer, call
from tests.support.native_consumer_entry import environment


@pytest.mark.parametrize("path", [
    "worker", "worker_forget", "worker_tool_forget", "search", "search_forget",
])
def test_inference_keeps_http_responsive_and_worker_fenced(core, tmp_path, monkeypatch, path):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls, inference_threads = [], []
    owner_thread = threading.get_ident()

    def infer(self, query, texts):
        inference_threads.append(threading.get_ident())
        if path == "worker_tool_forget" and not calls:
            return tuple(np.array([1., 0.]) for _ in texts), np.array([1., 0.])
        entered.set()
        try:
            assert threading.get_ident() != owner_thread
            assert release.wait(5)
            return tuple(np.array([1., 0.]) for _ in texts), np.array([1., 0.])
        finally:
            finished.set()

    monkeypatch.setattr(SemanticIndex, "_infer", infer)

    def provider(request):
        messages = json.loads(request.content)["messages"]
        calls.append(messages)
        body = answer("记得蓝色配色。") if any(m["role"] == "tool" for m in messages) else answer(
            calls=[call("recall-project", "memory_recall", {"query": "青禾项目使用什么配色"})])
        return httpx.Response(200, json=body)

    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "memory")) as (entry, _):
            client = entry.client
            source = await entry.admit(1, "青禾项目使用蓝色配色")
            with core.store.transaction():
                mid = core.memory.add(Candidate(subject="project", kind="fact", fact_key="color",
                    content=source["payload"]["text"], evidence_event_id=source["event_id"],
                    evidence_quote=source["payload"]["text"]), core.clock(), confirmed=True)
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model_http:
                with Core(core.settings, ModelClient(core.settings, model_http),
                          core.clock) as worker:
                    assert worker.store is not core.store
                    if path.startswith("worker"):
                        question = await entry.admit(2, "青禾项目使用什么配色")
                        task = asyncio.create_task(worker.run_once("independent-memory-worker"))
                    else:
                        task = asyncio.create_task(client.post("/internal/v1/memories/search",
                            json={"query": "青禾项目", "requester_actor_id": "owner", "limit": 6}))
                    try:
                        assert await asyncio.to_thread(entered.wait, 3)
                        assert all(tid != owner_thread for tid in inference_threads)
                        health = await asyncio.wait_for(client.get("/internal/v1/health/live"), 1)
                        assert health.status_code == 200 and not task.done()
                        if path.endswith("forget"):
                            forgotten = await client.post(f"/internal/v1/memories/{mid}/decisions",
                                json={"actor_id": "owner", "decision": "forget"})
                            assert forgotten.status_code == 200, forgotten.text
                        release.set()
                        result = await task
                        if path in {"worker_forget", "worker_tool_forget"}:
                            paid = path == "worker_tool_forget"
                            assert result and len(calls) == int(paid)
                            job = core.store.one("SELECT state,last_error_code FROM jobs "
                                "WHERE event_id=?", (question["event_id"],))
                            assert job == {"state": "CANCELLED",
                                           "last_error_code": (
                                               "REPLY_SOURCE_RECALLED" if paid
                                               else "REPLY_CANCELLED")}
                            assert not core.store.rows("SELECT * FROM memory_vectors")
                            assert core.store.rows("SELECT scenario,state FROM model_usage") == (
                                [{"scenario": "chat", "state": "SETTLED"}] if paid else [])
                            assert not core.store.rows("SELECT * FROM outbox")
                        elif path == "worker":
                            assert result and len(calls) == 2
                            assert "青禾项目使用蓝色配色" in json.dumps(
                                calls[-1], ensure_ascii=False)
                            assert core.store.rows("SELECT scenario,state FROM model_usage") == [
                                {"scenario": "chat", "state": "SETTLED"}] * 2
                        elif path == "search":
                            assert result.status_code == 200, result.text
                            assert result.json()["items"][0]["memory_id"] == mid
                        else:
                            assert result.status_code == 409, result.text
                            assert not core.store.rows("SELECT * FROM memory_vectors")
                        if path.startswith("search"):
                            assert not calls and not core.store.rows("SELECT * FROM model_usage")
                    finally:
                        release.set()
                        await asyncio.gather(task, return_exceptions=True)
                        assert await asyncio.to_thread(finished.wait, 3)
    run(scenario())
