"""A real Core worker with deterministic external model transport for C1 diagnostics."""

import asyncio
import json
import sys
from contextlib import closing
from pathlib import Path

import httpx

from companion import __main__, paid_budget
from companion.config import Settings
from companion.postgres import Database
from companion.store import Store

value = json.loads(Path(sys.argv[1]).read_text("utf-8"))
settings = Settings.model_validate(value["settings"])
calls = []


def provider(request):
    payload = json.loads(request.content)
    with (
        Database(settings.database) as database,
        closing(Store(database, settings.state_directory)) as store,
    ):
        row = store.one("SELECT measurement_json FROM model_admissions WHERE payload_sha256=%s",
                        (paid_budget.sha(payload),))
        assert row and json.loads(row["measurement_json"])["payload_sha256"] == (
            paid_budget.sha(payload))
    calls.append(payload)
    message = {"content": "independent meter reply"}
    if len(calls) == 1:
        message = {"content": None, "tool_calls": [{"id": "clock", "type": "function",
            "function": {"name": "current_time", "arguments": "{}"}}]}
    else:
        assert any(m["role"] == "tool" and m["tool_call_id"] == "clock"
                   for m in payload["messages"])
    usage = {"prompt_tokens": 100, "completion_tokens": 4}
    cache = value.get("cache_usage", [None, None])[len(calls) - 1]
    if cache is not None:
        usage["prompt_cache_hit_tokens"] = cache
    return httpx.Response(200, json={"choices": [{"message": message}], "usage": usage})


native_client = httpx.AsyncClient


class OfflineClient(native_client):
    def __init__(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(provider)
        super().__init__(*args, **kwargs)


__main__.httpx.AsyncClient = OfflineClient


async def exercise():
    task = asyncio.create_task(__main__.worker(settings))
    try:
        with (
            Database(settings.database) as database,
            closing(Store(database, settings.state_directory)) as observer,
        ):
            async with asyncio.timeout(25):
                while True:
                    if task.done():
                        await task
                        raise AssertionError("worker exited before completion")
                    job = observer.one("SELECT state,last_error_code FROM jobs WHERE job_id=%s",
                                       (value["job_id"],))
                    if calls and job["state"] in {"SUCCEEDED", "FAILED", "RETRY_WAIT"}:
                        assert job["state"] == "SUCCEEDED", job
                        break
                    await asyncio.sleep(0.02)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


asyncio.run(exercise())
assert len(calls) == 2
if "requests_path" in value:
    Path(value["requests_path"]).write_text(json.dumps(calls), "utf-8")
print(json.dumps({"mock_provider_requests": len(calls)}))
