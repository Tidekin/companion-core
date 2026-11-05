"""Native Core worker in an independent process, with synthetic-only HTTP transport."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import httpx

from companion.config import Settings
from companion.core import Core
from companion.model import ModelClient


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--now", required=True)
    parser.add_argument("--fence", action="store_true")
    parser.add_argument("--reply", default="SYNTHETIC owner runtime reply")
    args = parser.parse_args()
    raw = await asyncio.to_thread(Path(args.config).read_text, "utf-8")
    settings = Settings.model_validate_json(raw)
    if settings.model_url != "https://synthetic.invalid/v1" or (
        settings.model_key.get_secret_value() != "synthetic-key"
    ):
        raise ValueError("SYNTHETIC_PROCESS_ONLY")
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {
            "content": args.reply}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 2}})

    async def hook(request):
        if args.fence:
            print("BEFORE_DISPATCH", flush=True)
            async with asyncio.timeout(20):
                await asyncio.to_thread(sys.stdin.readline)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False,
                                 event_hooks={"request": [hook]}) as client:
        with patch("companion.retrieval.SemanticIndex.warmup", lambda self: None), Core(
            settings, ModelClient(settings, client), clock=lambda: args.now,
        ) as core:
            worked = await core.run_once("owner-runtime-process")
            print(json.dumps({"worked": worked, "calls": len(calls)}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
