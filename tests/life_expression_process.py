"""Independent native Life selector with only external model HTTP simulated."""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from companion.autonomous_selector import native_selector
from companion.config import Settings
from companion.core import Core
from companion.model import ModelClient


async def main():
    args = json.loads(await asyncio.to_thread(Path(sys.argv[1]).read_text, encoding="utf-8"))
    settings = Settings.model_validate(args["settings"])
    if settings.model_url != "https://synthetic.invalid/v1":
        raise ValueError("SYNTHETIC_PROVIDER_REQUIRED")
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "action": "SHARE", "destination": args["destination"],
                "source_event_ids": [args["source_id"]], "reason": "真实来源的合成表达",
                "text": args.get("wording") or "读过的恒星介绍让我想继续了解光谱与温度的关系。",
            })}}], "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with Core(settings, ModelClient(settings, client), lambda: args["now"]) as core:
            selector = native_selector(core, "independent-life-expression")
            for _ in range(len(selector.lanes) * 4):
                selected = await selector.step()
                if selected.blocked or not selected.more:
                    break
            else:
                raise AssertionError("native selector did not become idle")
            result = {"pid": os.getpid(), "provider_calls": len(calls),
                      "results": core.store.rows("SELECT * FROM life_activity_results"),
                      "jobs": core.store.rows("SELECT job_id,state,last_error_code FROM jobs")}
    await asyncio.to_thread(Path(args["result_path"]).write_text, json.dumps(result),
                            encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
