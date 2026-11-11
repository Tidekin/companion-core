"""Signed TCP ingress, independent CLI workers, tool continuation and saved recovery."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from companion.store import encode
from tests.conftest import run
from tests.support.native_consumer_entry import environment


@pytest.mark.parametrize("outcome", ["continuous", "resume", "withdraw"])
def test_immediate_sources_survive_actual_requests_and_restore(core, tmp_path, outcome):
    resume = outcome != "continuous"
    workspace = Path(__file__).resolve().parents[1]
    calls = []

    async def worker(label, mode="normal"):
        config = core.settings.model_dump(mode="json")
        for key in ("model_key", "bridge_token", "control_token", "database"):
            config[key] = getattr(core.settings, key).get_secret_value()
        path = tmp_path / (label + ".private.json")
        path.write_text(encode(config), encoding="utf-8")
        child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c",
            "import sys; from tests.support.memory_context_process import run_worker; "
            "run_worker(*sys.argv[1:])", str(path), core.clock(), mode, cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 60)
            assert child.returncode == 0, stderr.decode(errors="replace")
            result = json.loads(stdout.decode().splitlines()[-1])
            assert result["pid"] != os.getpid()
            calls.extend(result["calls"])
            return result
        finally:
            if child.returncode is None:
                child.kill()
                await child.communicate()

    async def scenario():
        async with environment(core, tmp_path, history_bytes=64000,
            core_options={"context_soft_tokens": 8000, "turn_recall_limit": 2,
                          "turn_recall_second_hop": 0},
            paid_options={"concurrency_basis": "local_active_attempts_v2"}) as (entry, sends):
            for number, text in enumerate([
                "补充背景。" * 160 + "台布是靛青。", "风铃闲聊。" * 700,
                "窗外闲聊。" * 700, "台布是砂黄。"], 1):
                await entry.admit(number, text)
                result = await worker("history-" + str(number))
                assert result["jobs"][-1]["state"] == "SUCCEEDED", result
                assert (await entry.send(entry.client, entry.protocol))["worked"]
                core.clock.advance(1)
            event = await entry.admit(5, "台布前后是什么颜色？两个说法都告诉我。")
            usage_before = core.store.rows("SELECT * FROM model_usage ORDER BY record_sequence")
            # A blocked restore must yield before a model reservation or tool call.
            core.settings.chat_restore_timeout_seconds = .1
            busy = await worker("busy", "busy_restore")
            assert busy["jobs"][-1]["state"] == "RETRY_WAIT", busy
            assert busy["jobs"][-1]["last_error_code"] == "CONVERSATION_RESTORE_BUSY"
            assert not busy["calls"]
            assert core.store.rows(
                "SELECT * FROM model_usage ORDER BY record_sequence") == usage_before
            core.settings.chat_restore_timeout_seconds = 5
            core.clock.advance(2)
            started = len(calls)
            result = await worker("exam", "pause_after_tool" if resume else "normal")
            job = core.store.one("SELECT * FROM jobs WHERE event_id=%s", (event["event_id"],))
            if resume:
                assert job["state"] == "RUNNING", result
                saved = core.store.one("SELECT phase FROM agent_conversations WHERE job_id=%s",
                                       (job["job_id"],))
                assert saved["phase"] == "READY"
                returned = core.store.rows("SELECT * FROM agent_tool_calls WHERE job_id=%s",
                                           (job["job_id"],))
                assert len(returned) == 1 and returned[0]["state"] == "RETURNED"
                if outcome == "withdraw":
                    usage = core.store.rows("SELECT * FROM model_usage ORDER BY record_sequence")
                    await entry.recall(1)
                    before = len(calls)
                    core.clock.advance(400)
                    result = await worker("withdrawn")
                    assert len(calls) == before and not result["calls"]
                    assert core.store.rows(
                        "SELECT * FROM model_usage ORDER BY record_sequence") == usage
                    assert core.store.one("SELECT phase FROM agent_conversations WHERE job_id=%s",
                                          (job["job_id"],))["phase"] == "STOPPED"
                    return
                # The worker stopped after the durable tool result. Existing lease
                # expiry recovers the same checkpoint and paid request identity.
                core.clock.advance(400)
                result = await worker("resume")
                assert core.store.rows("SELECT * FROM agent_tool_calls WHERE job_id=%s",
                                       (job["job_id"],)) == returned
            assert result["jobs"][-1]["state"] == "SUCCEEDED", result
            actual = calls[started:]
            assert len(actual) == 2
            for payload in actual:
                text = encode(payload["messages"])
                assert "靛青" in text and "砂黄" in text
                owners = [m.get("content") or "" for m in payload["messages"]
                          if m["role"] == "user"]
                assert sum("靛青" in value for value in owners) == 1
                assert sum("砂黄" in value for value in owners) == 1
            # Tool results retain their original contents. Initial source selection
            # itself must present each owner fact once, including the long original.
            first = encode(actual[0]["messages"])
            assert first.count("靛青") == first.count("砂黄") == 1
            snapshot = json.loads(core.store.one(
                "SELECT snapshots_json FROM agent_conversations WHERE job_id=%s",
                (job["job_id"],))["snapshots_json"])
            plan = next(item["metadata"]["core_context"] for item in snapshot
                        if item["kind"] == "context_plan")
            assert len(plan["requests"]) == 2
            assert any(item["kind"] == "history" for item in plan["assembly_dropped"])
            assert all(item["state"] == "SETTLED" for item in result["usage"])
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            assert len(sends) == 5

    run(scenario())
