"""One native chat-worker iteration in a child process, with offline provider transport."""

import asyncio
import json
import os
import sys
import threading
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx

from companion import __main__ as entrypoint
from companion import core as core_module
from companion.config import Settings
from companion.core import Core
from companion.retrieval import SemanticIndex
from companion.store import Store
from tests.sdk_reader_fixture import answer, call


def run_worker(settings_path, now):
    settings = Settings.model_validate_json(Path(settings_path).read_text("utf-8"))
    owner_thread = threading.get_ident()
    calls, inference_threads, violations = [], [], []
    original_client, original_run, original_infer = (
        httpx.AsyncClient,
        Core.run_once,
        SemanticIndex._infer,
    )

    class Finished(BaseException):
        pass

    def guard(method):
        def checked(self, *args, **kwargs):
            if threading.get_ident() != owner_thread:
                violations.append(method.__name__)
                raise AssertionError("Store crossed the inference thread boundary")
            return method(self, *args, **kwargs)

        return checked

    for name in ("execute", "rows", "one", "transaction"):
        setattr(Store, name, guard(getattr(Store, name)))

    def infer(self, query, contents):
        inference_threads.append(threading.get_ident())
        assert isinstance(query, str) and isinstance(contents, tuple)
        assert all(isinstance(text, str) for text in contents)
        assert threading.get_ident() != owner_thread
        if len(inference_threads) == 1:
            print(json.dumps({"phase": "inference_wait", "pid": os.getpid(),
                              "parent_pid": os.getppid(),
                              "inference_thread": threading.get_ident(),
                              "owner_thread": owner_thread}), flush=True)
            assert sys.stdin.readline().strip() == "continue"
        # Run the actual cached local encoder. Only the timing gate is synthetic.
        return original_infer(self, query, contents)

    def provider(request):
        assert request.url.host == "synthetic.invalid"
        messages = json.loads(request.content)["messages"]
        calls.append("chat")
        if any(message["role"] == "tool" for message in messages):
            assert "青禾项目使用蓝色配色" in json.dumps(messages, ensure_ascii=False)
            body = answer("青禾项目使用蓝色配色。")
        else:
            body = answer(calls=[call("recall-project-color", "memory_recall",
                                     {"query": "青禾项目使用什么配色"})])
        return httpx.Response(200, json=body)

    def offline_client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original_client(**kwargs)

    async def once(self, *args, **kwargs):
        await original_run(self, *args, **kwargs)
        print(json.dumps({"phase": "worker_result", "jobs": self.store.rows(
            "SELECT state,last_error_code FROM jobs")}), flush=True)
        raise Finished

    SemanticIndex._infer = infer
    Core.run_once = once
    core_module.Core = partial(Core, clock=lambda: now)
    entrypoint.httpx = SimpleNamespace(AsyncClient=offline_client)
    try:
        asyncio.run(entrypoint.worker(settings))
    except Finished:
        pass
    print(
        json.dumps(
            {
                "phase": "finished",
                "pid": os.getpid(),
                "owner_thread": owner_thread,
                "inference_threads": inference_threads,
                "store_thread_violations": violations,
                "calls": calls,
            }
        ),
        flush=True,
    )
