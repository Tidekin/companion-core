"""Independent native worker process with a mandatory synthetic provider transport."""

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import httpx

from companion.config import Settings
from companion.core import Core
from companion.model import ModelClient
from companion.paid_budget import sha


async def main():
    parser = argparse.ArgumentParser()
    for name in ("config", "now", "log", "ready", "gate"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--mode", choices=("normal", "crash"), default="normal")
    parser.add_argument("--drain", action="store_true")
    args = parser.parse_args()
    raw = await asyncio.to_thread(Path(args.config).read_text, "utf-8")
    settings = Settings.model_validate(json.loads(raw))
    if settings.model_url not in {
        "https://synthetic.invalid/v1", "https://api.openai.com/v1",
    } or (
        settings.model_key.get_secret_value() != "synthetic-key"
    ):
        raise ValueError("SYNTHETIC_PROCESS_ONLY")

    def provider(request):
        if settings.model_url == "https://api.openai.com/v1":
            payload = json.loads(request.content)
            assert "max_tokens" not in payload
            assert payload["max_completion_tokens"] == settings.max_output_tokens
        with Path(args.log).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"payload_sha256": sha(json.loads(request.content))}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if args.mode == "crash":
            os._exit(23)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "SYNTHETIC process reply"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 2048}})

    def wait_gate():
        deadline = time.monotonic() + 20
        while not Path(args.gate).exists():
            if time.monotonic() > deadline:
                raise TimeoutError("synthetic process gate")
            time.sleep(0.02)

    transport = httpx.MockTransport(provider)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with patch("companion.retrieval.SemanticIndex.warmup", lambda self: None), Core(
            settings, ModelClient(settings, client), clock=lambda: args.now
        ) as core:
            await asyncio.to_thread(Path(args.ready).write_text, "ready", encoding="utf-8")
            await asyncio.to_thread(wait_gate)
            worked = await core.run_once("synthetic-process-" + str(os.getpid()))
            while args.drain and core.store.one(
                "SELECT 1 FROM jobs WHERE kind IN ('chat','evaluation') "
                "AND state IN ('QUEUED','RUNNING')"
            ):
                # Another worker can own the preceding chat; retain the real
                # selector and wait until the queued experiment becomes eligible.
                await asyncio.sleep(0.02)
                worked = await core.run_once("synthetic-process-" + str(os.getpid())) or worked
            print(json.dumps({"worked": worked}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
