"""Actual native intake/delivery HTTP with separate worker and restart processes."""

import asyncio
import json
import os
import sys

import httpx
import pytest

from companion import paid_budget
from tests.conftest import run
from tests.poke_authority_fixture import environment, signed
from tests.test_evaluation_process import process_config
from tests.test_onebot_capture import real_core_http
from tests.test_reading_service import queued


@pytest.mark.parametrize("kind,outcome,stale_read", [
    ("text", "SENT", False), ("poke", "SENT", False), ("poke", "DELIVERY_UNCERTAIN", False),
    ("none", None, False), ("unknown", None, False), ("text", "SENT", True),
])
def test_native_poke_process_keeps_original_usage_and_never_replays(
    core, tmp_path, kind, outcome, stale_read,
):
    if stale_read:
        queued(core, owner=False)
        core.clock.advance(31)
        # Recover the historical parent-only state observed in production.
        core.store.execute("UPDATE exploration_requests SET state='UNKNOWN',"
                           "last_error_code='READ_TIMEOUT'")
    env = environment(core, tmp_path)
    capture = signed(env)
    config_path = tmp_path / "worker-config.json"
    process_config(core, config_path)
    if stale_read:
        values = json.loads(config_path.read_text("utf-8"))
        for name, adapter in core.settings.observation_adapters.items():
            values["observation_adapters"][name]["token"] = adapter.token.get_secret_value()
        config_path.write_text(json.dumps(values), encoding="utf-8")

    async def child(number):
        output = tmp_path / f"worker-{number}.json"
        process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
            "tests.poke_native_process", str(config_path), core.clock(), str(output), kind,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, errors = await asyncio.wait_for(process.communicate(), 45)
            assert process.returncode == 0, errors.decode(errors="replace")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        report = json.loads(output.read_text("utf-8"))
        assert report["pid"] != os.getpid()
        return report

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + env.config.bridge_token},
        ) as client:
            denied = await client.post("/internal/v1/pokes",
                json=capture.model_dump(mode="json"), headers={"Authorization": "Bearer wrong"})
            assert denied.status_code == 401
            accepted = await client.post("/internal/v1/pokes", json=capture.model_dump(mode="json"))
            assert accepted.status_code == 200, accepted.text
            assert accepted.headers["Cache-Control"] == "no-store"
            core.clock.advance(core.settings.poke_policy.coalesce_seconds + 1)
            first = await child(1)
            assert first["progressed"] and len(first["calls"]) == 1, first
            if stale_read:
                assert core.store.one("SELECT state FROM reading_attempts")["state"] == "UNKNOWN"
                assert core.store.one("SELECT used_requests FROM exploration_requests")[
                    "used_requests"] is None
            usage = core.store.rows("SELECT * FROM model_usage")
            assert len(usage) == 1 and usage[0]["scenario"] == "qq_poke"
            assert usage[0]["state"] == ("UNKNOWN" if kind == "unknown" else "SETTLED")
            assert usage[0]["billed_micro_usd"] > 0
            rows = core.store.rows("SELECT * FROM outbox")
            assert len(rows) == int(outcome is not None)
            if outcome is not None:
                leased = await client.post("/internal/v1/deliveries/lease", json={
                    "worker_id": "synthetic-poke-bridge", "authority": capture.authority})
                assert leased.status_code == 200, leased.text
                item = leased.json()["items"][0]
                path = "/internal/v1/deliveries/" + item["message_id"]
                begun = await client.post(path + "/begin-send",
                                          json={"lease_token": item["lease_token"]})
                assert begun.status_code == 200, begun.text
                checked = await client.post("/internal/v1/owner-authority/send-check", json={
                    "authority": capture.authority, "event_id": capture.event_id,
                    "message_id": item["message_id"], "content_sha256": item["content_sha256"],
                    "lease_token": item["lease_token"]})
                assert checked.status_code == 200, checked.text
                result = {"lease_token": item["lease_token"], "result": outcome}
                if outcome == "SENT":
                    if kind == "poke":
                        result["native_ack"] = {"kind": "onebot_action", "action": "friend_poke",
                            "status": "ok", "retcode": 0, "response_sha256": paid_budget.sha({
                                "status": "ok", "retcode": 0, "data": None})}
                    else:
                        result["provider_message_id"] = "synthetic-text-receipt"
                recorded = await client.post(path + "/result", json=result)
                assert recorded.status_code == 200, recorded.text
                assert (await client.post(path + "/result", json=result)).json() == recorded.json()
                assert core.store.one("SELECT state FROM outbox")["state"] == outcome
            delivered = core.store.rows("SELECT * FROM outbox")
            second = await child(2)
            assert second["calls"] == [] and not second["progressed"], second
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert core.store.rows("SELECT * FROM outbox") == delivered
            repeated = await client.post("/internal/v1/pokes", json=capture.model_dump(mode="json"))
            assert repeated.status_code == 200 and repeated.json()["duplicate"], repeated.text
            assert len(core.store.rows("SELECT * FROM jobs WHERE kind='poke'")) == 1

    run(scenario())
