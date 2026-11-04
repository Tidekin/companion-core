"""Real signed HTTP ingress, independent native workers, and owner-only inspection."""

import asyncio
import json
import sys

import httpx
from pydantic import SecretStr

from tests.support.core_http import real_core_http
from tests.test_evaluation_process import process_config
from tests.test_owner_input import captured, inbound

PATH = "/internal/v2/cognition/activity"


def test_signed_tcp_entry_independent_core_and_stop_leave_inspection_read_only(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    core.settings.control_token = SecretStr("synthetic-activity-owner-token")
    config = tmp_path / "activity-worker.json"
    process_config(core, config)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as client:
            bridge = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
            owner = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
            for headers in ({}, bridge, {"Authorization": "Bearer wrong-token"}):
                assert (await client.post(PATH, json={"conversation_id": "missing"},
                                          headers=headers)).status_code == 401
            approved = await client.post("/internal/v1/owner-authority/inputs/approve",
                json=capture.model_dump(mode="json"), headers=bridge)
            assert approved.status_code == 200
            event = inbound(core, capture)
            accepted = await client.post("/internal/v1/inbox/events",
                json=event.model_dump(mode="json"),
                headers={**bridge, "X-Idempotency-Key": "activity-native-input"})
            assert accepted.status_code == 202, accepted.text
            cid = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                 (event.event_id,))["conversation_id"]
            child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
                "tests.owner_runtime_process", "--config", str(config), "--now", core.clock(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(child.communicate(), 30)
                assert child.returncode == 0, stderr.decode(errors="replace")
                assert json.loads(stdout) == {"worked": True, "calls": 1}
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            stopped = await client.post("/internal/v1/owner-authority/stop", headers=owner,
                json={"authority": capture.authority, "operation_id": "activity-stop"})
            assert stopped.status_code == 200, stopped.text
            before = "\n".join(core.store.db.iterdump())
            observed = await client.post(PATH, json={"conversation_id": cid}, headers=owner)
            assert observed.status_code == 200, observed.text
            assert observed.headers["Cache-Control"] == "no-store"
            value = observed.json()
            assert value["mode"] == "SHADOW" and value["presence"]["state"] == "UNKNOWN"
            assert value["items"][0]["native_state"] == "SUCCEEDED"
            assert not value["items"][0]["lease"]["valid_at_as_of"]
            assert "synthetic owner input" not in observed.text
            assert "\n".join(core.store.db.iterdump()) == before
            for extra in ({"limit": True}, {"limit": 21}, {"actor": "owner"},
                          {"current_event_ids": [event.event_id]}):
                rejected = await client.post(PATH, json={"conversation_id": cid, **extra},
                                             headers=owner)
                assert rejected.status_code == 400
            missing = await client.post(PATH, json={"conversation_id": "missing"}, headers=owner)
            assert missing.status_code == 404

    asyncio.run(scenario())


def test_signed_read_and_isolated_reader_receipts_are_observed_without_more_io(
    core, tmp_path, monkeypatch,
):
    from tests import test_bili_video_entrypoint as reader_fixture
    from tests.test_reading_followup import TOKEN, URL, admit, select_read, window

    # The existing isolated Reader accepts only mocked upstream bytes and cannot
    # import Core or its database. Its Core HTTP calls use the original Reader token.
    monkeypatch.setattr(reader_fixture, "TOKEN", TOKEN)
    core.settings.control_token = SecretStr("synthetic-activity-reader-owner-token")
    isolated = reader_fixture.reader_files(tmp_path)
    metadata, _ = reader_fixture.upstream(tmp_path)

    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, config, plan):
            event, _ = await admit(client, core, config, plan, 1, card=URL)
            selected = await select_read(core, URL)
            cid = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                 (event["event_id"],))["conversation_id"]
            owner = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
            pending = await client.post(PATH, json={"conversation_id": cid}, headers=owner)
            assert pending.status_code == 200, pending.text
            assert any(item["kind"] == "read" for item in pending.json()["items"])
            completed = await reader_fixture.reader_once(isolated, str(client.base_url).rstrip("/"),
                                                        core.clock, metadata)
            assert completed["remaining"] == 0
            before = "\n".join(core.store.db.iterdump())
            observed = await client.post(PATH, json={"conversation_id": cid}, headers=owner)
            assert observed.status_code == 200, observed.text
            reads = [item for item in observed.json()["items"] if item["kind"] == "read"]
            assert len(reads) == 1 and reads[0]["attempt_state"] == "DONE"
            assert reads[0]["results"] and all(
                item["meaning"] == "OBSERVATION_RECEIPT" for item in reads[0]["results"])
            assert observed.json()["presence"]["state"] == "UNKNOWN"
            assert "字幕说" not in observed.text and "token=" not in observed.text
            assert "\n".join(core.store.db.iterdump()) == before
            assert len(selected.requests) == 1

    asyncio.run(scenario())
