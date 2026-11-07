import asyncio
import json
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx

from companion import life_worker
from companion.config import Settings
from companion.core import Core
from companion.model import EXTRACT_RULES, REVIEW_RULES
from companion.store import encode


def provider_response(text):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 0},
    })


def run_isolated_worker(settings_path, now):
    """Child process: actual loop and Core/store, fixed time, only provider HTTP mocked."""
    settings = Settings.model_validate_json(Path(settings_path).read_text("utf-8"))
    calls = []

    def provider(request):
        payload = json.loads(request.content)
        if payload["messages"][0]["content"] == REVIEW_RULES:
            calls.append("memory_review")
            candidates = json.loads(payload["messages"][-1]["content"])["candidates"]
            return provider_response(encode({"relations": [{"supported": True,
                "update_authorized": False, "claims_replacement": False,
                "same": None, "conflicts": [], "replaces": []} for _ in candidates]}))
        assert payload["messages"][0]["content"] == EXTRACT_RULES
        assert payload["max_tokens"] == min(4096, settings.paid_model.max_output_tokens)
        calls.append("memory_extract")
        sources = json.loads(payload["messages"][-1]["content"])
        source = next((row for row in sources["events"]
                       if row["text"] == "青禾项目下一步检查配色"), None)
        candidates = [{
            "subject": "project", "kind": "fact", "fact_key": "next_step",
            "content": "青禾项目下一步检查配色", "evidence_event_id": source["ref"],
            "evidence_quote": "青禾项目下一步检查配色",
        }] if source else []
        return provider_response(encode({"memories": candidates}))

    original_client = httpx.AsyncClient

    def offline_client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original_client(**kwargs)

    async def stop(*_args):
        raise asyncio.CancelledError

    life_worker.Core = partial(Core, clock=lambda: now)
    life_worker.httpx = SimpleNamespace(AsyncClient=offline_client)
    life_worker.Wakeup.wait = stop
    try:
        asyncio.run(life_worker.life_worker(settings))
    except asyncio.CancelledError:
        pass
    print(encode({"calls": calls}))
