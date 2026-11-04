"""Real owner HTTP edits while an independent production worker is mid-request."""

import asyncio
import hashlib
import json
import sys

import httpx
import pytest
from pydantic import SecretStr

from companion import runtime_config
from companion.owner_authority_protocol import OwnerCapture
from companion.qq_rich import input_digest
from companion.store import after, encode
from companion.turn_taking import SILENCE_RULES
from companion_delivery_protocol.authority import CAPTURE_DOMAIN
from tests.support.core_http import real_core_http
from tests.test_evaluation_process import process_config
from tests.test_owner_console import ORIGIN, PASSWORD
from tests.test_owner_input import captured, inbound


@pytest.mark.parametrize("change", ["safe", "expression", "mixed", "stop", "privacy",
                                    "busy", "closing"])
def test_native_worker_and_console_share_task_configuration_identity(core, tmp_path, change):
    capture, signing_key = captured(core, tmp_path)
    core.settings.operator_hot_reload = True
    core.settings.control_token = SecretStr(PASSWORD)
    core.settings.console.enabled = True
    core.settings.console.public_origin = ORIGIN
    if change == "closing":
        core.settings.input_debounce.enabled = True
        core.settings.input_debounce.quiet_seconds = 0.0
    config = tmp_path / "runtime-configuration.json"
    process_config(core, config)

    second_text = "好的" if change == "closing" else "synthetic owner input"

    def successor():
        value = capture.model_dump(mode="json", exclude={"signature"})
        value["source_event_id"] = capture.source_event_id.rsplit(":", 1)[0] + ":124"
        value["input_sha256"] = input_digest({"text": second_text})
        value["event_id"] = "evt_" + hashlib.sha256(value["source_event_id"].encode()).hexdigest()
        for field in ("received_at", "occurred_at"):
            value[field] = getattr(capture, field).isoformat()
        value["signature"] = signing_key.sign(CAPTURE_DOMAIN + encode(value).encode()).hex()
        return OwnerCapture.model_validate(value)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as client:
            async def accept(proof, text="synthetic owner input"):
                message = inbound(core, proof)
                message.payload.text = text
                approved = await client.post("/internal/v1/owner-authority/inputs/approve",
                                             json=proof.model_dump(mode="json"))
                assert approved.status_code == 200, approved.text
                response = await client.post("/internal/v1/inbox/events",
                    json=message.model_dump(mode="json"),
                    headers={"X-Idempotency-Key": proof.event_id})
                assert response.status_code == 202, response.text
                return response.json()["job_id"]

            first = await accept(capture)
            login = await client.post("/v1/console/session", json={"password": PASSWORD},
                                      headers={"Origin": ORIGIN})
            assert login.status_code == 200, login.text
            # The public cookie is Secure; this test's authenticated socket is HTTP loopback.
            cookie = login.headers["set-cookie"].split(";", 1)[0]
            headers = {"Origin": ORIGIN, "Cookie": cookie,
                       "X-CSRF-Token": login.json()["csrf_token"],
                       "Idempotency-Key": "process-save-runtime-configuration"}
            tasks = 1 if change in {"stop", "privacy"} else 2
            hot_change = change in {"safe", "expression", "busy", "closing"}
            next_now = after(core.clock(), 6)
            child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
                "tests.runtime_config_process", "--config", str(config), "--now", core.clock(),
                *(["--next-now", next_now] if change == "closing" else []),
                "--tasks", str(tasks), *(["--boundary-contention"] if change == "busy" else []),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                async with asyncio.timeout(45):
                    initial = (await child.stdout.readline()).strip()
                    if initial != b"BEFORE_REQUEST":
                        if child.returncode is None:
                            child.kill()
                        output, error = await child.communicate()
                        pytest.fail(f"Worker startup: {initial!r} {output!r} {error.decode()}")
                    changes = {"chat_time_labels": True}
                    if change == "expression":
                        changes.update(conversation_policy={"mode": "ENABLED"},
                            conversation_guidance="synthetic owner guidance",
                            style_live={"enabled": True})
                    if change == "closing":
                        changes.update(turn_taking={"silence_enabled": True},
                                       input_debounce={"active_seconds": 6})
                    if change == "mixed":
                        changes["monthly_limit_micro_usd"] = 99_999_999
                    saved = await client.put("/v1/console/configuration", json={
                        "expected_version": 0, "changes": changes}, headers=headers)
                    assert saved.status_code == 200, saved.text
                    assert saved.json()["active_version"] == 0
                    first_snapshot = runtime_config.get(core.store, core.settings, "job:" + first)
                    assert first_snapshot.version == 0
                    if change == "stop":
                        stopped = await client.post("/internal/v1/owner-authority/stop", json={
                            "authority": capture.authority,
                            "operation_id": "stop-reloading-worker"},
                            headers={"Authorization": "Bearer " + PASSWORD})
                        assert stopped.status_code == 200, stopped.text
                    elif change == "privacy":
                        with core.store.transaction():
                            core.ledger.expire_events(core.store, [capture.event_id], core.clock())
                    child.stdin.write(b"continue\n")
                    await child.stdin.drain()
                    assert (await child.stdout.readline()).strip() == b"COMPLETED"
                    if tasks == 2:
                        if change == "closing":
                            # Simulated transport delivered the first reply before the owner closed.
                            assert core.store.execute("UPDATE outbox SET state='SENT',sent_at=? "
                                "WHERE origin_event_id=? AND state='PENDING'",
                                (core.clock(), capture.event_id)).rowcount == 1
                        proof = successor()
                        second = await accept(proof, second_text)
                        if change == "closing":
                            assert core.store.one("SELECT next_attempt_at FROM jobs WHERE job_id=?",
                                                  (second,))["next_attempt_at"] == next_now
                            core.clock.advance(6)
                        child.stdin.write(b"next-task\n")
                        await child.stdin.drain()
                        assert (await child.stdout.readline()).strip() == b"BEFORE_REQUEST"
                        second_snapshot = runtime_config.get(core.store, core.settings,
                                                             "job:" + second)
                        assert second_snapshot.version == int(hot_change)
                        if change == "closing":
                            disabled = await client.put("/v1/console/configuration", json={
                                "expected_version": 1,
                                "changes": {"turn_taking": {"silence_enabled": False}}},
                                headers={**headers, "Idempotency-Key": "disable-during-closing"})
                            assert disabled.status_code == 200, disabled.text
                        child.stdin.write(b"continue\n")
                        await child.stdin.drain()
                        assert (await child.stdout.readline()).strip() == b"COMPLETED"
                    output, error = await child.communicate()
                    assert child.returncode == 0, error.decode()
                    result = json.loads(output)
                    assert result["busy_boundaries"] == int(change == "busy")
                    expected = [False, hot_change] if tasks == 2 else []
                    assert result["time_labels"] == expected
                    if change == "expression":
                        assert "synthetic owner guidance" not in result["systems"][0]
                        assert "synthetic owner guidance" in result["systems"][1]
                    if change == "closing":
                        assert SILENCE_RULES in result["systems"][1]
                        assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                                              (second,)) == {"state": "SUCCEEDED"}
                        assert not core.store.rows("SELECT 1 FROM outbox WHERE origin_event_id=?",
                                                   (proof.event_id,))
                        audit = core.store.one("SELECT safe_detail_json FROM audit_log "
                            "WHERE action='job.succeeded' AND entity_id=?", (second,))
                        assert json.loads(audit["safe_detail_json"])["reply_skipped"] is True
                    assert result["calls"] == (2 if tasks == 2 else 0)
                    assert runtime_config.get(core.store, core.settings, "job:" + first) == (
                        first_snapshot)
                    if tasks == 1:
                        assert core.store.rows("SELECT * FROM outbox") == []
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()

    asyncio.run(scenario())
