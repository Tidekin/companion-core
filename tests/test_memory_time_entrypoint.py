"""Temporal SDK schema through real signed TCP ingress and an independent CLI worker."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from companion.contracts import Candidate, EventTime
from companion.store import encode
from tests.conftest import run
from tests.support.native_consumer_entry import environment


@pytest.mark.parametrize("withdraw", [False, True])
def test_time_query_reaches_native_worker_without_unscoped_fallback(core, tmp_path, withdraw):
    workspace = Path(__file__).resolve().parents[1]

    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "memory"), history_bytes=0,
            paid_options={"per_request_calls": 2}) as (entry, _):
            text = "我计划在2026年1月1日去南门取钥匙"
            source = await entry.admit(900, text)
            with core.store.transaction(control_write=True):
                core.memory.add(Candidate(subject="project", kind="episode", fact_key="keys",
                    content=text, evidence_event_id=source["event_id"], evidence_quote=text,
                    event_time=EventTime(start="2026-01-01T00:00:00+08:00",
                        end="2026-01-02T00:00:00+08:00", precision="day", assertion="planned",
                        quote="2026年1月1日")), core.clock(), confirmed=True)
            if withdraw:
                await entry.recall(900)
            core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
            await entry.admit(901, "2026年1月1日我计划在哪取钥匙？")
            config = core.settings.model_dump(mode="json")
            for key in ("model_key", "bridge_token", "control_token", "database"):
                config[key] = getattr(core.settings, key).get_secret_value()
            path = tmp_path / "worker.private.json"
            path.write_text(encode(config), encoding="utf-8")
            child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c",
                "import sys; from tests.support.memory_context_process import run_worker; "
                "run_worker(*sys.argv[1:])", str(path), core.clock(), "temporal",
                cwd=workspace, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
            try:
                stdout, stderr = await asyncio.wait_for(child.communicate(), 60)
                assert child.returncode == 0, stderr.decode(errors="replace")
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.communicate()
            result = json.loads(stdout.decode().splitlines()[-1])
            assert result["pid"] != os.getpid()
            assert result["jobs"][-1]["state"] == "SUCCEEDED", {
                "jobs": result["jobs"], "stderr": stderr.decode(errors="replace")}
            assert len(result["calls"]) == 2
            tool = next(m for m in result["calls"][-1]["messages"] if m["role"] == "tool")
            value = json.loads(tool["content"])
            assert value["time_scope"]["basis"] == "occurred" and "raw_turns" not in value
            if withdraw:
                assert not value["facts"] and "南门" not in tool["content"]
            else:
                assert value["facts"][0]["event_time"]["assertion"] == "planned"
                assert value["facts"][0]["content"] == text
            assert all(item["state"] == "SETTLED" for item in result["usage"])

    run(scenario())
