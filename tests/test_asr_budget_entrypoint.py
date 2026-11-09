"""Selected native ASR evidence survives optional-work exhaustion in a real worker."""
import asyncio
import json
import os
import sys
from pathlib import Path

from companion import memory_references as refs
from companion.contracts import Candidate
from companion.store import encode
from tests.conftest import run
from tests.support.native_consumer_entry import environment


def test_selected_asr_reaches_provider_after_turn_search_consumes_its_allowance(core, tmp_path):
    workspace = Path(__file__).resolve().parents[1]

    async def worker(label, mode="normal"):
        config = core.settings.model_dump(mode="json")
        for key in ("database", "model_key", "bridge_token", "control_token"):
            config[key] = getattr(core.settings, key).get_secret_value()
        path = tmp_path / (label + ".private.json")
        path.write_text(encode(config), encoding="utf8")
        child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c",
            "import sys; from tests.support.context_selection_process import run_worker; "
            "run_worker(*sys.argv[1:])", str(path), core.clock(), mode,
            cwd=workspace,
            env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 60)
            assert child.returncode == 0, stderr.decode(errors="replace")
            result = json.loads(stdout.decode().splitlines()[-1])
            assert result["pid"] != os.getpid()
            return result
        finally:
            if child.returncode is None:
                child.kill()
                await child.communicate()

    async def scenario():
        async with environment(core, tmp_path, core_options={
            "relationship_standing_limit": 0, "turn_recall_limit": 2,
            "turn_recall_second_hop": 0}) as (entry, _):
            event = await entry.admit(1, "补一份松灯凭条的资料。")
            assert (await worker("source"))["jobs"][-1]["state"] == "SUCCEEDED"
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            raw = "今天8月7日，约好两天后到枫汀取松灯凭条。"
            receipt = refs.capture(core.memory, event["event_id"], "transcript", raw,
                                   {"fixture": "optional-work-exhaustion"}, core.clock())
            summary = "松灯凭条的转写日期被旧助手认为有矛盾。"
            key = core.memory.add(Candidate(subject="project", kind="fact", fact_key="receipt_date",
                content=summary, evidence_event_id=event["event_id"], evidence_quote="取松灯凭条",
                evidence_reference_id=receipt["reference_id"]), core.clock())
            stored = core.store.one("SELECT content FROM memories WHERE memory_id=%s", (key,))[
                "content"]
            core.clock.advance(60)
            question = await entry.admit(2, "松灯凭条约了哪天去取？")
            result = await worker("exam", "exhaust_turn_budget")
            job = core.store.one("SELECT state,last_error_code FROM jobs WHERE event_id=%s",
                                 (question["event_id"],))
            assert job["state"] == "SUCCEEDED", {"job": job, "result": result}
            assert result["recalled_facts"] and result["recalled_facts"][-1] > 0
            work = result["retrieval_work"][-1]
            assert 850 <= work["counts"]["sql_milliseconds"] <= 1000
            assert "stage:automatic_turns:sql_milliseconds" in work["reasons"]
            prompt = encode(result["calls"][-1]["messages"])
            assert raw in prompt and "机器转写原文摘录" in prompt
            assert summary not in prompt
            assert "本轮检索覆盖有限" in prompt
            assert all(row["state"] == "SETTLED" for row in result["usage"])
            assert core.store.one("SELECT content FROM memories WHERE memory_id=%s", (key,))[
                "content"] == stored
    run(scenario())
