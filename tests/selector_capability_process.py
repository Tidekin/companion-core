"""Original selector process consumes native capability jobs; provider HTTP is synthetic."""

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
from tests.support.meme_core import CAPTION


async def main():
    config_path, now, output, target = sys.argv[1:]
    settings = Settings.model_validate_json(
        await asyncio.to_thread(Path(config_path).read_text, encoding="utf-8"))
    if (settings.model_url != "https://synthetic.invalid/v1"
        or target not in {"poke", "meme_caption"}):
        raise ValueError("SYNTHETIC_CAPABILITY_WORKER_REQUIRED")
    calls, turns = [], []

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(CAPTION if target == "meme_caption" else {"kind": "none"})}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with Core(settings, ModelClient(settings, client), lambda: now) as core:
            selector = native_selector(core, "native-capability-selector")
            for _ in range(4 * len(selector.lanes)):
                before = len(calls)
                selected = await selector.step()
                assert len(calls) - before <= 1
                turns.append({"lane": selected.lane, "progressed": selected.result.progressed})
                if selected.blocked or not selected.more:
                    break
            else:
                raise AssertionError("native capability selector did not reach idle")
    await asyncio.to_thread(Path(output).write_text,
        json.dumps({"pid": os.getpid(), "calls": calls, "turns": turns}), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
