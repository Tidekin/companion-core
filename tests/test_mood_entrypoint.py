"""Real capture/HTTP, independent original worker and mock OneBot delivery."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from companion.store import encode
from tests.conftest import run
from tests.support.native_consumer_entry import environment


@pytest.mark.parametrize("hot_reload", [False, True])
@pytest.mark.parametrize("appraisal,pleasure,arousal,reason", [
    ("这个回答很有帮助", .08, .02, "HELPFUL_FEEDBACK"),
    ("这个想法很有意思，我们继续聊聊", .025, .02, "SHARED_IDEA_INTEREST"),
    ("这个观点我不认同", 0, .01, "VIEW_DISAGREEMENT"),
])
def test_native_chat_worker_carries_mood_through_neutral_chat_and_hours(
    core, tmp_path, hot_reload, appraisal, pleasure, arousal, reason,
):
    core.settings.operator_hot_reload = hot_reload
    workspace = Path(__file__).resolve().parents[1]

    async def worker(label):
        config = core.settings.model_dump(mode="json")
        for key in ("model_key", "bridge_token", "control_token"):
            config[key] = getattr(core.settings, key).get_secret_value()
        path = tmp_path / (label + ".json")
        path.write_text(encode(config), encoding="utf-8")
        child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c",
            "import sys; from tests.support.mood_process import run_worker; "
            "run_worker(sys.argv[1], sys.argv[2])", str(path), core.clock(),
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 60)
            assert child.returncode == 0, stderr.decode(errors="replace")
            value = json.loads(stdout.decode().splitlines()[-1])
            assert value["pid"] != os.getpid()
            assert all(job["state"] == "SUCCEEDED" for job in value["jobs"]), value
            assert len(value["calls"]) == 1, value
            return value["calls"][0]
        finally:
            if child.returncode is None:
                child.kill()
                await child.communicate()

    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "expression", "history"),
                               core_options={"mood_enabled": True}) as (entry, sends):
            await entry.admit(1, "一起看看这个问题")
            assert await worker("first") == []
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            assert len(sends) == 1
            core.clock.advance(1)
            await entry.admit(2, appraisal)
            positive = await worker("feedback")
            assert positive[0]["state"]["pleasure"] == pleasure
            assert positive[0]["state"]["arousal"] == arousal
            assert "causes" not in positive[0]
            snapshots = json.loads(core.store.one(

                    'SELECT snapshots_json FROM agent_conversations ORDER BY '
                    'record_sequence DESC LIMIT 1'

            )["snapshots_json"])
            proof = next(item["proof"] for item in snapshots if item["kind"] == "mood")
            assert proof["signals"][0]["reason"] == reason
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            core.clock.advance(3 * 3600)
            await entry.admit(3, "聊一聊今天的阅读吧")
            carried = await worker("hours-later")
            assert carried[0]["state"]["pleasure"] == pytest.approx(pleasure / 2, abs=1e-6)
            assert carried[0]["state"]["arousal"] == pytest.approx(arousal / 2, abs=1e-6)
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            assert len(sends) == 3
            assert core.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == 3

    run(scenario())
