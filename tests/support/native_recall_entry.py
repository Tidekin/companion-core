"""Signed native inputs and SDK memory recall over actual bounded retrieval services."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx

from companion.contracts import Candidate
from tests.sdk_reader_fixture import answer, call, worker_for
from tests.support.native_consumer_entry import environment

FACT = "本次临时项目的验收代号是青竹七号。"
QUESTION = "本次临时项目的验收代号是什么？"


def returned(payload):
    return [message for message in payload["messages"] if message["role"] == "tool"]


@asynccontextmanager
async def native_recall(core, tmp_path, *, respond=None, paid=None, core_options=None,
                        request_hook=None, query=QUESTION, fact_text=FACT, tool_query=None,
                        additional_purposes=(), history_bytes=0):
    calls, scores = [], []

    def provider(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if respond:
            return respond(request, payload, len(calls))
        body = answer("青竹七号。") if returned(payload) else answer(calls=[
            call("recall-project", "memory_recall", {"query": tool_query or query})])
        return httpx.Response(200, json=body)

    async with environment(core, tmp_path,
        purposes=("chat", "memory", *additional_purposes, *(["history"] if history_bytes else [])),
        history_bytes=history_bytes, history_start_offset=1 if history_bytes else -1,
        core_options={"max_recent_messages": 30 if history_bytes else 0,
                      **(core_options or {})},
        paid_options={"per_request_calls": 2, **(paid or {})}) as (entry, sends):
        ids, sources = [], []
        for number, text in ((900, fact_text), (901, "另一个公开项目代号是紫云三号。")):
            source = await entry.admit(number, text)
            sources.append(source)
            with core.store.transaction():
                ids.append(core.memory.add(Candidate(subject="project", kind="fact",
                    fact_key=str(number), content=text, evidence_event_id=source["event_id"],
                    evidence_quote=text), core.clock(), confirmed=True))
        # The native owner_private scope excludes public facts from retrieval.
        core.store.execute("UPDATE memories SET privacy_scope='public' WHERE memory_id=?",
                           (ids[1],))
        if history_bytes:
            # The actual grant starts dialogue history after the separate memory evidence seeds.
            core.clock.advance(1)
        number = 0

        async def capture(text=query, *, message=None):
            nonlocal number
            number += 1
            return await entry.admit(number, text, message=message)

        async with worker_for(core, provider) as worker:
            original = worker.memory.index.scores_async

            async def observe(query, rows, **kwargs):
                scores.append([row["memory_id"] for row in rows])
                return await original(query, rows, **kwargs)

            worker.memory.index.scores_async = observe
            if request_hook:
                worker.model.client.event_hooks["request"].append(request_hook)
            yield SimpleNamespace(entry=entry, worker=worker, capture=capture,
                calls=calls, sends=sends, scores=scores, memory_id=ids[0], outsider=ids[1],
                source=sources[0], query=query)
