"""An independent native extraction worker; only provider transport is substituted."""

import asyncio
import json
import os
from pathlib import Path

import httpx

from companion.config import Settings
from companion.core import Core
from companion.model import ModelClient
from tests.sdk_reader_fixture import answer


def run_worker(settings_path, now):
    settings = Settings.model_validate_json(Path(settings_path).read_text("utf-8"))
    assert settings.model_url == "https://synthetic.invalid/v1"
    calls = []

    def provider(request):
        assert request.url.host == "synthetic.invalid"
        payload = json.loads(request.content)
        calls.append(payload)
        data = json.loads(payload["messages"][-1]["content"])
        if "events" in data:
            event = next(row for row in data["events"] if any(
                ref["kind"] == "image" for ref in row["references"]))
            reference = next(ref for ref in event["references"] if ref["kind"] == "image")
            text = json.dumps({"memories": [{"subject": "project", "kind": "fact",
                "fact_key": "card_entry", "content": "青栎资料卡记载十二月九日入场，座位B17。",
                "evidence_event_id": event["ref"], "evidence_quote": "十二月九日入场",
                "evidence_reference_id": reference["ref"]}]}, ensure_ascii=False)
        else:
            text = json.dumps({"relations": [
                {"supported": True, "update_authorized": False, "claims_replacement": False,
                    "same": None, "conflicts": [], "replaces": []}
                for _ in data["candidates"]]})
        return httpx.Response(200, json=answer(text))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            with Core(settings, ModelClient(settings, client), clock=lambda: now) as core:
                assert await core.extract_once()
                print(json.dumps({"pid": os.getpid(), "calls": calls,
                    "memories": core.store.rows("SELECT * FROM memories"),
                    "usage": core.store.rows("SELECT scenario,state FROM model_usage")}),
                    flush=True)

    asyncio.run(scenario())
