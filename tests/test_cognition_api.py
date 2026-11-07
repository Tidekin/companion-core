"""Real API owner inspection of independent native worker accounting after STOP."""

import asyncio
import json
import sys

import httpx
from pydantic import SecretStr

from tests.support.core_http import real_core_http
from tests.support.owner_input import captured, inbound
from tests.test_evaluation_process import process_config


def test_native_cognition_http_reads_worker_receipt_after_stop(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    core.settings.control_token = SecretStr("synthetic-cognition-owner-token")
    config = tmp_path / "cognition-worker.json"
    process_config(core, config)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as client:
            path = "/internal/v2/cognition"
            bridge = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
            owner = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
            for headers in ({}, bridge, {"Authorization": "Bearer wrong-token"}):
                assert (await client.post(path, json={"kind": "model"},
                                          headers=headers)).status_code == 401
            approved = await client.post("/internal/v1/owner-authority/inputs/approve",
                json=capture.model_dump(mode="json"), headers=bridge)
            assert approved.status_code == 200, approved.text
            accepted = await client.post("/internal/v1/inbox/events",
                json=inbound(core, capture).model_dump(mode="json"),
                headers={**bridge, "X-Idempotency-Key": "cognition-worker-input"})
            assert accepted.status_code == 202, accepted.text
            child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
                "tests.owner_runtime_process", "--config", str(config), "--now", core.clock(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                async with asyncio.timeout(30):
                    output, error = await child.communicate()
                    assert child.returncode == 0, error.decode()
                    assert json.loads(output) == {"worked": True, "calls": 1}
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            stopped = await client.post("/internal/v1/owner-authority/stop", json={
                "authority": capture.authority, "operation_id": "stop-cognition-inspection"},
                headers=owner)
            assert stopped.status_code == 200, stopped.text
            before = core.store.rows("SELECT * FROM model_usage")
            outbox = core.store.rows("SELECT * FROM outbox")
            response = await client.post(path, json={"kind": "model"}, headers=owner)
            assert response.status_code == 200, response.text
            assert response.headers["Cache-Control"] == "no-store"
            result = response.json()
            assert result["reasoning"] == "NOT_EXPOSED"
            assert len(result["items"]) == 1
            item = result["items"][0]
            assert item["state"] == "SETTLED"
            assert item["id"] == before[0]["request_id"]
            assert item["occurred_at"] == before[0]["occurred_at"]
            assert item["usage"]["input_tokens"] == 30
            assert item["usage"]["output_tokens"] == 2
            assert item["measurement"]["state"] == "RECORDED"
            assert "synthetic owner input" not in response.text
            assert "SYNTHETIC owner runtime reply" not in response.text
            assert core.store.rows("SELECT * FROM model_usage") == before
            assert core.store.rows("SELECT * FROM outbox") == outbox
            for invalid in ({"kind": "model", "limit": 51}, {"kind": "arbitrary"},
                            {"kind": "model", "actor": "owner"}):
                rejected = await client.post(path, json=invalid, headers=owner)
                assert rejected.status_code == 400
            for kind in ("wake", "life", "goal", "diary", "reflection", "nightly"):
                response = await client.post(path, json={"kind": kind}, headers=owner)
                assert response.status_code == 200, response.text

    asyncio.run(scenario())
