"""The shipped worker loop, isolated clock and mocked provider transport only."""

import argparse
import asyncio
import json
import sys
import traceback
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx
import psycopg

from companion import operator_config, runtime_config
from companion.__main__ import worker
from companion.config import Settings
from companion.core import Core
from companion.turn_taking import SILENCE_RULES, SKIP


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--now", required=True)
    parser.add_argument("--next-now")
    parser.add_argument("--tasks", type=int, default=2)
    parser.add_argument("--boundary-contention", action="store_true")
    args = parser.parse_args()
    raw = await asyncio.to_thread(Path(args.config).read_text, "utf-8")
    settings = operator_config.load(Settings.model_validate_json(raw))
    if settings.model_url != "https://synthetic.invalid/v1" or (
        settings.model_key.get_secret_value() != "synthetic-key"
    ):
        raise ValueError("SYNTHETIC_PROCESS_ONLY")
    calls, completed, busy_boundaries = [], 0, 0
    native_boundary = runtime_config.boundary

    def boundary(store, config, clock):
        nonlocal busy_boundaries
        if args.boundary_contention and completed == 1 and not busy_boundaries:
            with closing(psycopg.connect(config.database.get_secret_value())) as writer:
                writer.execute(
                    "SELECT 1 FROM runtime_meta WHERE key='privacy_epoch' FOR UPDATE")

                try:
                    return native_boundary(store, config, clock)
                except psycopg.errors.LockNotAvailable as error:
                    assert error.sqlstate == "55P03" and not store.in_transaction
                    busy_boundaries += 1
                    raise
                finally:
                    writer.rollback()
        return native_boundary(store, config, clock)

    async def gate():
        async with asyncio.timeout(25):
            await asyncio.to_thread(sys.stdin.readline)

    async def hook(request):
        print("BEFORE_REQUEST", flush=True)
        await gate()

    def provider(request):
        calls.append(json.loads(request.content))
        may_skip = SILENCE_RULES in calls[-1]["messages"][0]["content"]
        reply = SKIP if may_skip else "synthetic configuration reply"
        return httpx.Response(200, json={"choices": [{"message": {
            "content": reply}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 2}})

    class SyntheticCore(Core):
        def __init__(self, settings, model):
            super().__init__(settings, model, clock=lambda: args.now)

        def fail_job(self, job, code):
            traceback.print_exception(sys.exception(), file=sys.stderr)
            return super().fail_job(job, code)

        async def run_once(self, *worker_args, **kwargs):
            nonlocal completed
            result = await super().run_once(*worker_args, **kwargs)
            terminal = self.store.one("SELECT COUNT(*) n FROM jobs WHERE state IN "
                                      "('SUCCEEDED','FAILED','CANCELLED')")["n"]
            if terminal > completed:
                completed = terminal
                print("COMPLETED", flush=True)
                if completed < args.tasks:
                    await gate()
                    if args.next_now:
                        args.now = args.next_now
            return result

    original = httpx.AsyncClient

    class SyntheticClient(original):
        def __init__(self, **options):
            options.setdefault("transport", httpx.MockTransport(provider))
            options.setdefault("event_hooks", {"request": [hook]})
            super().__init__(**options)

    with (patch("companion.core.Core", SyntheticCore),
          patch("companion.runtime_config.boundary", boundary),
          patch("companion.__main__.httpx.AsyncClient", SyntheticClient),
          patch("companion.shutdown.requested", lambda: completed >= args.tasks),
          patch("companion.retrieval.SemanticIndex.warmup", lambda self: None)):
        await worker(settings)
    print(json.dumps({"calls": len(calls), "busy_boundaries": busy_boundaries,
        "systems": [call["messages"][0]["content"] for call in calls], "time_labels": [any(
        "当前时间参考" in str(message["content"]) for message in call["messages"])
        for call in calls]}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
