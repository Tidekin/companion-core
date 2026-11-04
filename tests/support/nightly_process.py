"""Independent native Life worker with synthetic provider HTTP and controlled time."""

import asyncio
import json
import os
from functools import partial
from pathlib import Path

import httpx

from companion import life_worker, memory_work
from companion.config import Settings
from companion.core import Core
from companion.diary import DIARY_RULES
from companion.model import EXTRACT_RULES, REVIEW_RULES
from companion.reflection import REFLECTION_RULES
from companion.store import encode


def run_worker(settings_path, now, stop_after_extract):
    settings = Settings.model_validate_json(Path(settings_path).read_text("utf-8"))
    calls, units = [], []

    class Finished(Exception):
        pass

    def provider(request):
        assert request.url.host == "synthetic.invalid"
        messages = json.loads(request.content)["messages"]
        rules, data = messages[0]["content"], json.loads(messages[-1]["content"])
        if rules == EXTRACT_RULES:
            calls.append("memory_extract")
            events = data["events"]
            value = {"memories": [
                {"subject": "project", "kind": "fact", "fact_key": "source-" + str(index),
                 "content": row["text"], "evidence_event_id": row["ref"],
                 "evidence_quote": row["text"], "salience": (8, 3)[index]}
                for index, row in enumerate(events[:2])
            ], "episode_summaries": [{"title": "项目安排",
                "summary": "讨论了两个项目的安排，并计划周末核对资料；这些仍是待办事项。",
                "source_event_ids": [events[0]["group"]]}]}
        elif rules == REVIEW_RULES:
            calls.append("memory_review")
            value = {"relations": [
                {"supported": True, "update_authorized": False, "claims_replacement": False,
                    "same": None, "conflicts": [], "replaces": []}
                for _ in data["candidates"]]}
        elif rules == DIARY_RULES:
            calls.append("diary")
            source = next(row for row in data["sources"] if row["kind"] == "owner_message")
            value = {"schema_version": 1, "local_date": data["local_date"],
                     "title": "真实交流记录", "worth_saving": True,
                     "sections": [{"kind": "observation", "text": source["text"],
                                   "evidence": [{"source_id": source["source_id"],
                                                 "exact_quote": source["text"]}]}]}
        else:
            assert rules == REFLECTION_RULES
            calls.append("reflection")
            value = {"suggestions": []}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": encode(value)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 30},
        })

    class OfflineClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            # Keep the SDK's client type intact and preserve ModelClient's
            # explicit admission transport around this mock provider.
            kwargs.setdefault("transport", httpx.MockTransport(provider))
            super().__init__(*args, **kwargs)

    original_selector = life_worker.native_selector

    def observed_selector(core, worker_id):
        selector = original_selector(core, worker_id)
        original_step = selector.step

        async def measured():
            before = core.store.one("SELECT COUNT(*) AS n FROM model_usage")["n"]
            selected = await original_step()
            admitted = core.store.one("SELECT COUNT(*) AS n FROM model_usage")["n"] - before
            assert admitted <= 1
            units.append({"lane": selected.lane, "progressed": selected.result.progressed,
                          "admissions": admitted})
            record = memory_work.checkpoint_state(core.store)
            if stop_after_extract and record and record["batch"].get("phase") == "REVIEW":
                raise Finished
            return selected

        selector.step = measured
        return selector

    async def stop(*args, **kwargs):
        raise asyncio.CancelledError

    life_worker.native_selector = observed_selector
    life_worker.Core = partial(Core, clock=lambda: now)
    life_worker.httpx.AsyncClient = OfflineClient
    life_worker.Wakeup.wait = stop
    try:
        asyncio.run(life_worker.life_worker(settings))
    except (Finished, asyncio.CancelledError):
        pass
    print(encode({"pid": os.getpid(), "calls": calls, "units": units}), flush=True)
