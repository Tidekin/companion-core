"""Real owner HTTP control and independent worker dispatch/reset replay boundaries."""

import asyncio
import hashlib
import json
import sys

import httpx
from cryptography.fernet import Fernet
from pydantic import SecretStr

from companion import owner_authority, owner_input
from companion.affect import NEUTRAL
from companion.backup import backup, restore
from companion.conversation_policy import ConversationPolicyConfig
from companion.owner_authority_protocol import OwnerCapture, OwnerGrantPolicy
from companion.qq_rich import input_digest
from companion_delivery_protocol.authority import capture_signing_payload
from tests.conftest import ingest
from tests.support.core_http import real_core_http
from tests.support.evaluation_process import process_config
from tests.support.owner_authority import activate
from tests.support.owner_input import captured, inbound


def v1_policy(**values):
    return ConversationPolicyConfig(strategy="participation_v1", **values)


async def child(core, tmp_path, mode):
    config = tmp_path / (mode + ".json")
    process_config(core, config)
    return await asyncio.create_subprocess_exec(sys.executable, "-B", "-m", "tests.p1_process",
        "--config", str(config), "--now", core.clock(), "--mode", mode,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)


def signed_ingest(core, template, key, number, text):
    value = template.model_dump(mode="json", exclude={"signature"})
    source = template.source_event_id.rpartition(":")[0] + ":" + str(number)
    value.update(source_event_id=source,
                 event_id="evt_" + hashlib.sha256(source.encode()).hexdigest(),
                 input_sha256=input_digest({"text": text}),
                 occurred_at=core.clock(), received_at=core.clock())
    value["signature"] = key.sign(capture_signing_payload(value)).hex()
    capture = OwnerCapture.model_validate(value)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    message = inbound(core, capture)
    message = message.model_copy(update={"occurred_at": capture.occurred_at,
        "payload": message.payload.model_copy(update={"text": text})})
    return core.ingest(message, "p1-source-" + str(number))


def prepare(core, tmp_path):
    template, key = captured(core, tmp_path)
    policy = owner_authority.current(core.store, core.settings, core.clock).policy
    granted, _ = activate(core, OwnerGrantPolicy.model_validate({
        **policy.model_dump(), "purposes": ["chat", "affect"],
    }), revision=1, operation="operation-p1-affect")
    template = template.model_copy(update={"authority": granted["authority"]})
    core.settings.affect_enabled = True
    core.settings.control_token = SecretStr("synthetic-owner-control-" * 3)
    accepted = signed_ingest(core, template, key, 1, "简短一点")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (accepted["job_id"],))
    assert core.affect.snapshot()["evidence"]
    core.clock.advance(1)
    return template, key


def test_inbox_only_requests_exclusive_control_for_the_owner_reset_command(core, monkeypatch):
    lock = core.store._control_lock
    observed = []

    def record(exclusive):
        observed.append(exclusive)
        return lock(exclusive)

    monkeypatch.setattr(core.store, "_control_lock", record)
    for number, (text, exclusive) in enumerate((
        ("普通问候", False),
        ("/重置互动 附加参数", False),
        ("/重置互动", True),
    ), 1):
        observed.clear()
        accepted = ingest(core, number, text)
        assert accepted["state"] == "QUEUED"
        assert observed == [exclusive]


def test_owner_reset_after_context_before_dispatch_across_processes(core, tmp_path):
    template, key = prepare(core, tmp_path)
    core.settings.conversation_policy = v1_policy(mode="ENABLED")
    signed_ingest(core, template, key, 2, "怎么处理这个问题")

    async def scenario():
        process = await child(core, tmp_path, "fence")
        try:
            async with asyncio.timeout(35), real_core_http(core) as port, httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False,
            ) as client:
                assert (await process.stdout.readline()).strip() == b"CONTEXT_READY"
                denied = await client.post("/internal/v2/affect/reset",
                    json={"request_id": "reset-process"}, headers={"Authorization":
                        "Bearer " + core.settings.bridge_token.get_secret_value()})
                assert denied.status_code == 401
                reset = await client.post("/internal/v2/affect/reset",
                    json={"request_id": "reset-process"}, headers={"Authorization":
                        "Bearer " + core.settings.control_token.get_secret_value()})
                assert reset.status_code == 200, reset.text
                assert reset.headers["Cache-Control"] == "no-store"
                stdout, stderr = await process.communicate(b"continue\n")
                assert process.returncode == 0, stderr.decode()
                value = json.loads(stdout)
                assert value["calls"] == [] and value["outbox"] == []
                assert value["affect"]["state"] == NEUTRAL
        finally:
            if process.returncode is None:
                process.kill()
            await process.communicate()
    asyncio.run(scenario())


def test_reset_command_and_old_backup_replay_in_independent_core(core, tmp_path, empty_database):
    template, signing_key = prepare(core, tmp_path)
    key, archive, destination = Fernet.generate_key(), tmp_path / "old.enc", empty_database
    backup(core.settings.database, archive, key)
    signed_ingest(core, template, signing_key, 2, "/重置互动")

    async def run(mode):
        process = await child(core, tmp_path, mode)
        try:
            async with asyncio.timeout(30):
                stdout, stderr = await process.communicate()
                assert process.returncode == 0, stderr.decode()
                return json.loads(stdout)
        finally:
            if process.returncode is None:
                process.kill()
            await process.communicate()

    value = asyncio.run(run("command"))
    assert value["calls"] == [] and value["affect"]["state"] == NEUTRAL
    assert "已重置互动反馈" in value["outbox"][0]["content_json"]
    restore(archive, destination, key, core.settings.erasure_ledger,
            state_directory=tmp_path / "restored-state")
    core.settings.database = destination
    core.settings.state_directory = str(tmp_path / "restored-state")
    value = asyncio.run(run("inspect"))
    assert value["calls"] == [] and value["affect"]["state"] == NEUTRAL
