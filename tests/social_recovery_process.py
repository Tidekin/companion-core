
"""Crash at native planning boundaries in a separate synthetic-only process."""

import asyncio
import json
import os
import sys
from contextlib import closing
from pathlib import Path

import httpx

from companion import budget
from companion.autonomous_selector import native_selector
from companion.config import Settings
from companion.model import ModelClient
from companion.postgres import Database
from companion.social_autonomy import SocialAutonomy
from companion.social_autonomy_runtime import run_once
from companion.store import Store, encode
from tests.social_model_transport import next_social_turn


async def main():
    args = json.loads(await asyncio.to_thread(Path(sys.argv[1]).read_text, encoding="utf-8"))
    settings = Settings.model_validate(args["settings"])
    if settings.mode == "live" and settings.model_url != "https://synthetic.invalid/v1":
        raise ValueError("SYNTHETIC_PROVIDER_REQUIRED")
    mode = args["mode"].removeprefix("retry_")
    original_reserve, original_complete = budget.reserve, budget.complete

    def reserve(*args, **kwargs):
        if mode == "before_reserve":
            os._exit(23)
        rid = original_reserve(*args, **kwargs)
        if mode == "reserved":
            os._exit(23)
        return rid

    async def complete(*args, **kwargs):
        result = await original_complete(*args, **kwargs)
        if mode == "settled":
            os._exit(23)
        return result

    calls, payloads = [], []

    def provider(request):
        calls.append(True)
        payloads.append(json.loads(request.content)["messages"])
        return httpx.Response(200, json={
            "choices": [{"message": {"content": encode(
                {"kind": "nickname", "value": "星语"} if mode == "core_profile" else {
                    "kind": "qzone_publish", "text": "合成公开阅读想法。",
                    "source_refs": [args["source_id"]],
                }
            )}}], "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    if mode in {"core_defer", "core_resume", "core_profile"}:
        from companion import cloud_budget
        from companion.contracts import CoreError
        from companion.core import Core
        from companion.retrieval import SemanticIndex

        SemanticIndex.warmup = lambda self: None
        if mode == "core_defer":
            def refuse(*args, **kwargs):
                raise CoreError("CLOUD_DAILY_BUDGET_EXHAUSTED", 429)
            cloud_budget.reserve = refuse
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            with Core(settings, ModelClient(settings, client), lambda: args["now"]) as worker:
                selector = native_selector(worker, mode)
                worked = await next_social_turn(selector)
                idle = not await next_social_turn(selector)
        await asyncio.to_thread(Path(args["result_path"]).write_text, json.dumps({
            "pid": os.getpid(), "worked": worked, "idle": idle, "provider_calls": len(calls),
            "payloads": payloads,
        }), encoding="utf-8")
        return

    budget.reserve, budget.complete = reserve, complete
    with (
        Database(settings.database) as database,
        closing(Store(database, settings.state_directory)) as store,
    ):
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            module = SocialAutonomy(store, settings, ModelClient(settings, client),
                                    lambda: args["now"])
            if args["mode"].startswith("retry_"):
                await run_once(store, settings, module.model, module.clock, worker_id="child")
            else:
                await module.plan("crashed-social-plan", source_ids=[args["source_id"]])


if __name__ == "__main__":
    asyncio.run(main())
