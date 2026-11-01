"""Real HTTP API or independent production worker, with only provider transport mocked."""

import asyncio
import json
import os
import sys
from contextlib import closing
from pathlib import Path

import httpx
import uvicorn

from companion import gateway, paid_budget
from companion.config import Settings
from companion.gateway_api import create_app
from companion.model import ModelClient
from companion.store import Store

settings = Settings.model_validate_json(Path(sys.argv[2]).read_bytes())

if sys.argv[1] == "api":
    uvicorn.run(create_app(settings), host="127.0.0.1", port=int(sys.argv[3]),
                access_log=False, proxy_headers=False, log_level="error")
else:
    observed = []
    if sys.argv[1] == "worker-paused":
        original_journal = paid_budget.journal

        def paused_journal(store, approval, record=None, **kwargs):
            result = original_journal(store, approval, record, **kwargs)
            if record and record.get("kind") == sys.argv[4]:
                print("GATEWAY_JOURNAL_APPENDED", flush=True)
                if sys.stdin.readline().strip() != "continue":
                    raise RuntimeError("synthetic journal gate was not released")
            return result

        paid_budget.journal = paused_journal

    def provider(request):
        observed.append(json.loads(request.content))
        if sys.argv[1] == "crash":
            Path(sys.argv[3]).write_text(json.dumps(observed), encoding="utf-8")
            os._exit(23)
        return httpx.Response(200, json={"choices": [{"index": 0, "message": {
            "role": "assistant", "content": "independent worker reply"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    async def run():
        with closing(Store(settings.database)) as store:
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                await gateway.run_once(store, settings, ModelClient(settings, client))

    asyncio.run(run())
    Path(sys.argv[3]).write_text(json.dumps(observed), encoding="utf-8")
