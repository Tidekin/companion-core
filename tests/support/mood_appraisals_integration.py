import asyncio
import json
import os
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx

from companion import life_worker
from companion.config import Settings
from companion.core import Core
from companion.store import encode
from tests.conftest import ingest
from tests.support.next_memory_worker import provider_response


def prepare_extraction(core, *, quote="想你了", mood_enabled=True):
    core.settings.memory_enabled = True
    core.settings.mood_enabled = mood_enabled
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    key = ingest(core, 1, "想你了，什么时候再聊")["event_id"]
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.model.extraction = encode({"memories": [], "interaction_signals": [{
        "event_id": key, "reason": "OWNER_AFFECTION", "quote": quote,
    }]})
    return key


def run_isolated_worker(settings_path, now):
    """Actual life-worker entry point; only the external model HTTP transport is mocked."""
    settings = Settings.model_validate_json(Path(settings_path).read_text("utf-8"))
    calls = []
    original_client = httpx.AsyncClient

    def provider(request):
        assert request.url.host == "synthetic.invalid"
        payload = json.loads(request.content)
        sources = json.loads(payload["messages"][-1]["content"])
        # The extractor sees short refs (e0, ...); Core maps them back to event IDs.
        key = next(row["ref"] for row in sources["events"] if row["text"].startswith("想你了"))
        calls.append("memory_extract")
        return provider_response(encode({"memories": [], "interaction_signals": [{
            "event_id": key, "reason": "OWNER_AFFECTION", "quote": "想你了",
        }]}))

    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(provider))

    async def stop(*_args):
        raise asyncio.CancelledError

    life_worker.Core = partial(Core, clock=lambda: now)
    life_worker.httpx = SimpleNamespace(AsyncClient=client)
    life_worker.Wakeup.wait = stop
    try:
        asyncio.run(life_worker.life_worker(settings))
    except asyncio.CancelledError:
        pass
    print(encode({"calls": calls, "pid": os.getpid()}))
