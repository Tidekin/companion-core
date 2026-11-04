"""Real signed-input HTTP and independent native worker; all provider I/O is mocked."""

import asyncio
import json
import sys

import httpx
import pytest
from pydantic import SecretStr

from tests.support.core_http import real_core_http
from tests.test_evaluation_process import process_config
from tests.test_owner_input import captured, inbound


@pytest.mark.parametrize("stop", [False, True])
def test_real_api_input_and_worker_final_authority_fence(core, tmp_path, stop):
    capture, _ = captured(core, tmp_path)
    core.settings.control_token = SecretStr("synthetic-owner-control-token")
    config = tmp_path / "owner-runtime.json"
    process_config(core, config)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as client:
            approved = await client.post("/internal/v1/owner-authority/inputs/approve",
                                          json=capture.model_dump(mode="json"))
            assert approved.status_code == 200, approved.text
            accepted = await client.post("/internal/v1/inbox/events",
                json=inbound(core, capture).model_dump(mode="json"),
                headers={"X-Idempotency-Key": "owner-process-input"})
            assert accepted.status_code == 202, accepted.text
            child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
                "tests.owner_runtime_process", "--config", str(config), "--now", core.clock(),
                *(["--fence"] if stop else []), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                async with asyncio.timeout(30):
                    if stop:
                        assert (await child.stdout.readline()).strip() == b"BEFORE_DISPATCH"
                        stopped = await client.post("/internal/v1/owner-authority/stop", json={
                            "authority": capture.authority,
                            "operation_id": "operation-process-stop"},
                            headers={"Authorization": "Bearer synthetic-owner-control-token"})
                        assert stopped.status_code == 200, stopped.text
                    output, error = await child.communicate(b"continue\n" if stop else None)
                    assert child.returncode == 0, error.decode()
                    assert json.loads(output)["calls"] == (0 if stop else 1)
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            lease = await client.post("/internal/v1/deliveries/lease", json={
                "worker_id": "owner-process-bridge", "authority": capture.authority, "limit": 1})
            if stop:
                assert lease.status_code == 403
                assert core.store.rows("SELECT * FROM outbox") == []
            else:
                assert lease.status_code == 200, lease.text
                item = lease.json()["items"][0]
                assert item["authority"] == capture.authority
                begun = await client.post(
                    f"/internal/v1/deliveries/{item['message_id']}/begin-send",
                    json={"lease_token": item["lease_token"]})
                assert begun.status_code == 200, begun.text
                checked = await client.post("/internal/v1/owner-authority/send-check", json={
                    "authority": item["authority"], "event_id": item["origin_event_id"],
                    **{key: item[key] for key in ("message_id", "content_sha256", "lease_token",
                                                  "reply_revision")}})
                assert checked.status_code == 200, checked.text
                assert checked.headers["Cache-Control"] == "no-store"
    asyncio.run(scenario())
    usage = core.store.one("SELECT state,billed_micro_usd FROM model_usage")
    assert usage["state"] == "SETTLED"
    if stop:
        assert usage["billed_micro_usd"] == 0
        assert core.store.one("SELECT 1 FROM audit_log WHERE action='model.not_dispatched'")
    else:
        assert usage["billed_micro_usd"] > 0
