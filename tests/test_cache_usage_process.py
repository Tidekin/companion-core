"""Signed HTTP, an independent native worker, owner reads and native delivery ACK."""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from companion import beta_observation, budget, paid_budget
from companion.store import encode
from tests.support.native_consumer_entry import environment
from tests.support.token_measurement import configure
from tests.support.voice_node_api import tls_core


def test_signed_entry_and_worker_keep_absent_cache_distinct_from_explicit_zero(core, tmp_path):
    configure(core, tmp_path)
    core.settings.console.enabled = True
    core.settings.console.public_origin = "https://console.example.test"

    async def exercise():
        async with environment(core, tmp_path, purposes=("chat",), paid_options={
            "provider": "deepseek", "model": "deepseek-flash",
            "endpoint": "https://api.deepseek.com",
        }) as (entry, sends):
            source = await entry.admit(1, "synthetic cache presence inspection")
            job_id = entry.accepted[source["event_id"]]["job_id"]
            settings = core.settings.model_dump(mode="json")
            for name in ("model_key", "bridge_token", "control_token"):
                settings[name] = getattr(core.settings, name).get_secret_value()
            path, requests_path = tmp_path / "cache-worker.json", tmp_path / "requests.json"
            path.write_text(encode({"settings": settings, "job_id": job_id,
                "cache_usage": [None, 0], "requests_path": str(requests_path)}), "utf-8")
            child = await asyncio.create_subprocess_exec(sys.executable, "-B",
                str(Path(__file__).with_name("context_meter_worker.py")), str(path),
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(child.communicate(), 40)
            except BaseException:
                if child.returncode is None:
                    child.kill()
                await child.wait()
                raise
            assert child.returncode == 0, stderr.decode("utf-8", errors="replace")
            assert json.loads(stdout) == {"mock_provider_requests": 2}
            requests = json.loads(requests_path.read_text("utf-8"))
            rows = core.store.rows("SELECT m.*,a.payload_sha256,a.measurement_json,"
                "a.input_bound,a.output_bound,a.reserved_micro_usd FROM model_usage m "
                "JOIN model_admissions a USING(request_id) WHERE m.job_id=? ORDER BY m.rowid",
                (job_id,))
            assert len(rows) == 2
            for row, payload in zip(rows, requests, strict=True):
                assert row["state"] == "SETTLED"
                assert (row["input_tokens"], row["output_tokens"],
                        row["cached_input_tokens"]) == (100, 4, 0)
                assert row["billed_micro_usd"] == budget.cost(core.settings, 100, 4, 0)
                measured = json.loads(row["measurement_json"])
                assert row["payload_sha256"] == measured["payload_sha256"] == (
                    paid_budget.sha(payload))
                assert row["output_bound"] == measured["output_allowance"] == payload["max_tokens"]
                assert row["input_bound"] == (
                    measured["input_tokens"] + measured["reservation_margin"])
                assert row["reserved_micro_usd"] == budget.cost(
                    core.settings, row["input_bound"], row["output_bound"])
                assert "cache_usage_present" not in encode(payload)
                assert "user_id" not in payload
            # Read through the actual authenticated HTTP entry point, not an alternate DTO.
            trace_id = core.store.one("SELECT trace_id FROM events WHERE event_id=?",
                                      (source["event_id"],))["trace_id"]
            calls = beta_observation.inspect_context(core.settings, trace_id)["trace"]["jobs"][0][
                "model_calls"]
            before = core.store.rows("SELECT * FROM model_usage ORDER BY request_id")
            async with tls_core(core, tmp_path) as (url, _), httpx.AsyncClient(
                base_url=url, verify=False, trust_env=False,
            ) as console:
                origin = core.settings.console.public_origin
                login = await console.post("/v1/console/session",
                    json={"password": core.settings.control_token.get_secret_value()},
                    headers={"Origin": origin})
                assert login.status_code == 200, login.text
                for row, call, expected in zip(rows, calls, [None, 0], strict=True):
                    result = await console.get("/v1/console/cognition", params={
                        "kind": "model", "record_id": row["request_id"]})
                    assert result.status_code == 200, result.text
                    usage = result.json()["items"][0]["usage"]
                    assert usage["observed"] is True and usage["cached_input_tokens"] == 0
                    assert usage["cache_usage"] == call["cache_usage"] == {
                        "state": "NOT_RECORDED" if expected is None else "RECORDED",
                        "cached_input_tokens": expected}
                    assert call["prompt_cache_hit_tokens"] == expected
                    assert (call["prompt_tokens"], call["completion_tokens"],
                        call["configured_price_micro_usd"]) == (100, 4, row["billed_micro_usd"])
            sent = await entry.send(entry.client, entry.protocol)
            assert sent["worked"] and len(sends) == 1
            assert sent["attempt_states"] == {"ACK": 1}
            assert core.store.one("SELECT state FROM outbox WHERE origin_event_id=?",
                                  (source["event_id"],)) == {"state": "SENT"}
            assert core.store.rows("SELECT * FROM model_usage ORDER BY request_id") == before

    asyncio.run(exercise())
