"""Real HTTP acceptance, process-restarted native wording and synthetic delivery ACK."""

import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from companion import owner_authority
from companion.store import encode
from tests import native_input_protocol_fixture as native
from tests.conftest import run
from tests.support.owner_authority import activate


@pytest.mark.parametrize("first_text,delivery", [("[SKIP]", "SENT"), (" ", "DELIVERY_UNCERTAIN")])
def test_required_wording_retries_in_new_process_without_replaying_paid_request(
    core,
    tmp_path,
    monkeypatch,
    first_text,
    delivery,
):
    core.settings.goals_enabled = core.settings.exploration_enabled = True
    core.settings.control_token = SecretStr("synthetic-notice-owner-" * 3)
    settings_path = tmp_path / "worker-settings.json"
    workspace = Path(__file__).resolve().parents[1]

    async def worker(text):
        script = (
            "import sys; from tests.support.required_notice_process import run_worker; "
            "run_worker(sys.argv[1], sys.argv[2], sys.argv[3])"
        )
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            "-c",
            script,
            str(settings_path),
            core.clock(),
            text,
            cwd=workspace,
            env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
            assert child.returncode == 0, stderr.decode("utf-8", errors="replace")
            result = json.loads(stdout.decode("utf-8").splitlines()[-1])
            assert result["pid"] != os.getpid()
            assert child.pid in {result["pid"], result["parent_pid"]}
            return result["calls"]
        finally:
            if child.returncode is None:
                child.kill()
                await child.communicate()

    async def scenario():
        async with native.environment(core, tmp_path, monkeypatch) as values:
            client, config, grant, now = values
            current = owner_authority.current(core.store, core.settings, core.clock)
            policy = current.policy.model_copy(update={"purposes": ["chat", "goals"]})
            grant.authority = activate(core, policy, revision=grant.authority["revision"],
                operation="required-notice-purpose")[0]["authority"]
            settings = core.settings.model_dump(mode="json")
            for name in ("database", "model_key", "bridge_token", "control_token"):
                settings[name] = getattr(core.settings, name).get_secret_value()
            settings_path.write_text(encode(settings), encoding="utf-8")
            source, accepted = await native.admit(client, config, grant, now)
            assert accepted.status_code == 202, accepted.text
            core.store.execute(
                "UPDATE jobs SET state='CANCELLED',"
                "last_error_code='SYNTHETIC_BACKGROUND_ONLY' WHERE kind='chat'"
            )
            commitment = await client.post(
                "/internal/v2/commitments",
                json={
                    "proposal": {
                        "topic": "阅读资料",
                        "source_event_ids": [source["event_id"]],
                        "scope": {"operation": "read_content", "url": "https://example.org/a"},
                    },
                    "accepted_event_id": source["event_id"],
                    "deadline": (core.clock.now + timedelta(seconds=60)).isoformat(),
                },
                headers={
                    "Authorization": "Bearer " + core.settings.control_token.get_secret_value()
                },
            )
            assert commitment.status_code == 202, commitment.text
            core.clock.advance(61)
            assert core.goals.claim() is None
            job = core.store.one("SELECT * FROM jobs WHERE kind='activity'")
            assert await worker(first_text) == ["EXPLAINED_FAILURE"]
            first = core.store.one("SELECT * FROM model_usage")
            assert first["state"] == "SETTLED" and first["billed_micro_usd"] > 0
            assert not core.store.rows("SELECT * FROM outbox")
            assert await worker("还未到下一次尝试时间") == []
            core.clock.advance(6)
            assert await worker("这次没有在约定时间内完成阅读。") == ["EXPLAINED_FAILURE"]
            usages = core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence')
            assert len(usages) == 2 and usages[0] == first
            assert all(row["state"] == "SETTLED" and row["billed_micro_usd"] > 0 for row in usages)
            admissions = core.store.rows('SELECT * FROM model_admissions ORDER BY record_sequence')
            assert len({row["request_id"] for row in admissions}) == 2
            assert {row["logical_request_id"] for row in admissions} == {job["job_id"]}
            assert [row["attempt_number"] for row in admissions] == [1, 2]
            assert core.store.one("SELECT state FROM commitments")["state"] == "IN_PROGRESS"
            lease = await client.post(
                "/internal/v1/deliveries/lease",
                json={"worker_id": "synthetic", "authority": grant.authority}
            )
            assert lease.status_code == 200, lease.text
            (item,) = lease.json()["items"]
            path = "/internal/v1/deliveries/" + item["message_id"]
            begun = await client.post(
                path + "/begin-send", json={"lease_token": item["lease_token"]}
            )
            assert begun.status_code == 200, begun.text
            acknowledged = await client.post(
                path + "/result",
                json={
                    "lease_token": item["lease_token"],
                    "result": delivery,
                },
            )
            assert acknowledged.status_code == 200, acknowledged.text
            state = core.store.one("SELECT state FROM commitments")["state"]
            assert state == ("EXPLAINED_FAILURE" if delivery == "SENT" else "IN_PROGRESS")
            assert await worker("不得再次产生通知") == []
            assert core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence') == usages
            assert core.store.one("SELECT COUNT(*) n FROM outbox")["n"] == 1

    run(scenario())
