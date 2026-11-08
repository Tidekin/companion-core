"""Real HTTP ingestion and a separate native worker; external model transport is mocked."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from companion import paid_budget
from tests.support.native_consumer_entry import environment
from tests.support.token_measurement import configure

WORKER = r'''
import asyncio, json, sys
from contextlib import closing
from pathlib import Path
import httpx
from companion import __main__
from companion.config import Settings
from companion.postgres import Database
from companion.store import Store

value = json.loads(Path(sys.argv[1]).read_text("utf-8"))
settings = Settings.model_validate(value["settings"])
assert settings.model_key.get_secret_value() == "synthetic-key"
assert settings.token_measurement.policy == "prefer_exact"
calls = []
def provider(request):
    calls.append(json.loads(request.content))
    result = {"choices": [{"message": {"content": "synthetic reply"}}]}
    if value["outcome"] != "missing_usage":
        result["usage"] = {"prompt_tokens": 100, "completion_tokens": 4}
    if value["outcome"] == "invalid_reply":
        result["choices"] = []
    return httpx.Response(200, json=result)
native_client = httpx.AsyncClient
class OfflineClient(native_client):
    def __init__(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(provider)
        super().__init__(*args, **kwargs)
__main__.httpx.AsyncClient = OfflineClient
async def exercise():
    task = asyncio.create_task(__main__.worker(settings))
    try:
        with (Database(settings.database) as database,
              closing(Store(database, settings.state_directory)) as observer):
            async with asyncio.timeout(20):
                while True:
                    if task.done():
                        await task
                        raise AssertionError("worker exited before the native job completed")
                    job = observer.one("SELECT state FROM jobs WHERE job_id=%s", (value["job_id"],))
                    if calls and job["state"] in {"SUCCEEDED", "FAILED", "RETRY_WAIT"}:
                        break
                    await asyncio.sleep(0.02)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
asyncio.run(exercise())
assert len(calls) == 1
print(json.dumps({"model_requests": len(calls)}))
'''


@pytest.mark.parametrize("outcome", ["success", "invalid_reply", "missing_usage"])
def test_http_to_independent_worker_count_and_accounting(core, tmp_path, outcome):
    configure(core, tmp_path)
    config_path = tmp_path / "worker.json"

    async def exercise():
        async with environment(core, tmp_path, purposes=("chat",), paid_options={
            "provider": "deepseek", "model": "deepseek-flash",
            "endpoint": "https://api.deepseek.com",
        }) as (entry, _):
            source = await entry.admit(1, "synthetic token measurement request")
            settings = core.settings.model_dump(mode="json")
            settings["database"] = core.settings.database.get_secret_value()
            settings["model_key"] = "synthetic-key"
            settings["bridge_token"] = core.settings.bridge_token.get_secret_value()
            settings["control_token"] = core.settings.control_token.get_secret_value()
            config_path.write_text(json.dumps({"settings": settings, "outcome": outcome,
                "job_id": entry.accepted[source["event_id"]]["job_id"]}), "utf-8")
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-B", "-c", WORKER, str(config_path),
                cwd=Path(__file__).parents[1], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(child.communicate(), 30)
            except BaseException:
                if child.returncode is None:
                    child.kill()
                await child.wait()
                raise
            assert child.returncode == 0, stderr.decode("utf-8", errors="replace")
            assert json.loads(stdout) == {"model_requests": 1}

    asyncio.run(exercise())
    row = core.store.one("SELECT a.*,m.state,m.input_tokens,m.output_tokens FROM "
                         "model_admissions a JOIN model_usage m USING(request_id)")
    measured = json.loads(row["measurement_json"])
    assert measured["count_kind"] == "exact_local_render"
    assert measured["payload_sha256"] == row["payload_sha256"]
    assert row["input_bound"] == measured["input_tokens"] + measured["reservation_margin"]
    assert row["state"] == ("UNKNOWN" if outcome == "missing_usage" else "SETTLED")
    if outcome != "missing_usage":
        assert (row["input_tokens"], row["output_tokens"]) == (100, 4)
    if outcome == "invalid_reply":
        assert core.store.one("SELECT last_error_code FROM jobs")["last_error_code"] == (
            "MODEL_INVALID_RESPONSE")
        # Core may emit its native failure notice, but never the rejected model reply.
        assert "synthetic reply" not in json.dumps(core.store.rows("SELECT * FROM outbox"))
    with core.store.transaction():
        paid_budget.journal(core.store, core.settings.paid_model, allow_settlement=True)
