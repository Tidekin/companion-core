"""One actual caption worker process, with synthetic provider transport only."""

import asyncio
import json
import sys
from pathlib import Path

import httpx

from companion.config import Settings
from companion.core import Core
from companion.model import ModelClient
from companion.retrieval import SemanticIndex
from tests.support.meme_core import CAPTION

settings_path, now, output = sys.argv[1:]
settings = Settings.model_validate_json(Path(settings_path).read_text("utf-8"))
SemanticIndex.warmup = lambda self: None
calls = []


def provider(request):
    calls.append(json.loads(request.content))
    return httpx.Response(200, json={"choices": [{"message": {
        "content": json.dumps(CAPTION)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


async def run():
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with Core(settings, ModelClient(settings, client), lambda: now) as worker:
            progressed = await worker.run_once("native-caption-process")
            return {"progressed": progressed, "calls": calls}


Path(output).write_text(json.dumps(asyncio.run(run())), encoding="utf-8")
