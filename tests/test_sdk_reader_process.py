"""HTTP capture and Reader result resume the original SDK job in fresh processes."""

import asyncio
import json
import pickle
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from companion import operator_config, runtime_config
from tests.conftest import run
from tests.sdk_reader_fixture import answer, call
from tests.support.exploration import URL
from tests.support.reading_followup import admit, finish, lease, window


@pytest.mark.parametrize("hot_reload", [False, True])
def test_reader_result_resumes_one_job_without_repeating_provider_or_reader(
    core, tmp_path, monkeypatch, hot_reload,
):
    core.settings.operator_hot_reload = hot_reload
    if hot_reload:
        core.settings.control_token = SecretStr("synthetic-reader-config-" * 3)

    async def step(job_id, responses, state, index):
        config = tmp_path / f"reader-worker-{index}.pickle"
        output = tmp_path / f"reader-worker-{index}.json"
        settings = operator_config.load(core.settings) if hot_reload else core.settings
        config.write_bytes(pickle.dumps((settings, core.clock.now, job_id,
                                        responses, state, str(output))))
        process = await asyncio.create_subprocess_exec(sys.executable,
            str(Path(__file__).with_name("reader_sdk_worker.py")), str(config),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            logs, _ = await asyncio.wait_for(process.communicate(), 45)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, logs.decode(errors="replace")
        return json.loads(output.read_text(encoding="utf-8"))

    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, config, plan):
            event, accepted = await admit(client, core, config, plan, 1, card=URL)
            assert not core.store.one("SELECT 1 FROM exploration_requests")
            first = await step(accepted["job_id"], [answer(calls=(
                call("read-original", "read_content", {"url": URL}),
            ))], "RETRY_WAIT", 1)
            assert len(first) == 1
            from companion.context_planner import check_bound
            from companion.privacy import epoch

            job = core.store.one('SELECT * FROM jobs WHERE job_id=%s', (accepted["job_id"],))
            checkpoint = core.store.one("SELECT phase,snapshots_json FROM agent_conversations")
            assert checkpoint["phase"] == "WAITING"
            original_configuration = None
            if hot_reload:
                original_configuration = runtime_config.check(core.store, core.settings,
                                                             "job:" + accepted["job_id"])
                operator_config.update(core.store, core.settings,
                    operator_config.ConfigurationChange(expected_version=0,
                        changes={"chat_time_labels": True,
                                 "conversation_policy": {"mode": "ENABLED"},
                                 "conversation_guidance": "new guidance after Reader wait",
                                 "style_live": {"enabled": True}}),
                    "reader-wait-config-save", core.clock())
                assert operator_config.load(core.settings).chat_time_labels
            plan = check_bound(core.store, job, json.loads(checkpoint["snapshots_json"]),
                               epoch(core.store))
            assert len(plan["requests"]) == 1 and plan["pending"] is None
            assert not core.store.one("SELECT 1 FROM outbox")
            request = core.store.one("SELECT * FROM exploration_requests")
            item = await lease(client)
            assert item["request_id"] == request["request_id"]
            receipt, body = await finish(client, core, item)
            assert receipt["state"] == "DONE"
            # The same authenticated result receipt is idempotent.
            from tests.support.reading_followup import READER_AUTH

            duplicate = await client.post(
                "/internal/v3/readers/bili/" + item["request_id"] + "/result",
                json=body, headers=READER_AUTH)
            assert duplicate.status_code == 200 and duplicate.json()["duplicate"]
            second = await step(accepted["job_id"], [answer("根据原读取回执回答。")],
                                "SUCCEEDED", 2)
            assert len(second) == 1
            if hot_reload:
                assert runtime_config.check(core.store, core.settings,
                    "job:" + accepted["job_id"]) == original_configuration
                assert not any("当前时间参考" in str(row["content"])
                               for row in second[0]["messages"])
                assert "new guidance after Reader wait" not in json.dumps(second[0]["messages"])
            assert any(row["role"] == "tool" and row.get("tool_call_id") == "read-original"
                       for row in second[0]["messages"])
            assert await lease(client) is None
            assert len(core.store.rows("SELECT * FROM reading_attempts")) == 1
            assert len(core.store.rows("SELECT * FROM exploration_requests")) == 1
            assert len(core.store.rows("SELECT * FROM model_usage WHERE state='SETTLED'")) == 2
            assert core.store.rows("SELECT origin_event_id FROM outbox") == [
                {"origin_event_id": event["event_id"]}]
            assert core.store.one("SELECT phase FROM agent_conversations")["phase"] == "COMPLETE"
            checkpoint = core.store.one("SELECT snapshots_json FROM agent_conversations")
            plan = check_bound(core.store, job, json.loads(checkpoint["snapshots_json"]),
                               epoch(core.store))
            assert len(plan["requests"]) == 2 and plan["pending"] is None

    run(scenario())
