"""Real signed capture/TCP ingress, independent native worker and final provider payload."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from companion.contracts import Candidate
from companion.goals import GoalProposal, GoalScope
from companion.model import SOURCE_RULES
from companion.store import encode
from tests.conftest import run
from tests.support.goals import result_for
from tests.support.native_consumer_entry import environment
from tests.support.observation_capture import enable_observations
from tests.support.subjective_cognition import ASTRONOMY, judge


@pytest.mark.parametrize("hot_reload", [False, True])
def test_native_context_selection_keeps_evidence_origin_and_recent_dialogue(core, tmp_path,
                                                                          hot_reload):
    enable_observations(core, source="bilibili")
    fact = "实际获得的天文内容 1"
    workspace = Path(__file__).resolve().parents[1]
    calls = []

    async def worker(label):
        config = core.settings.model_dump(mode="json")
        for key in ("database", "model_key", "bridge_token", "control_token"):
            config[key] = getattr(core.settings, key).get_secret_value()
        for name, adapter in core.settings.observation_adapters.items():
            config["observation_adapters"][name]["token"] = adapter.token.get_secret_value()
        path = tmp_path / (label + ".json")
        path.write_text(encode(config), encoding="utf-8")
        child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c",
            "import sys; from tests.support.context_selection_process import run_worker; "
            "run_worker(sys.argv[1], sys.argv[2])", str(path), core.clock(), cwd=workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            stdout, stderr = await asyncio.wait_for(child.communicate(), 60)
            assert child.returncode == 0, stderr.decode(errors="replace")
            value = json.loads(stdout.decode().splitlines()[-1])
            assert value["pid"] != os.getpid()
            assert all(job["state"] == "SUCCEEDED" for job in value["jobs"]), value
            assert all(row["state"] == "SETTLED" for row in value["usage"])
            # Existing memory relevance review can precede chat. Selection adds no call.
            assert 1 <= len(value["calls"]) <= 2, value
            calls.extend(value["calls"])
            return value["calls"][-1]["messages"]
        finally:
            if child.returncode is None:
                child.kill()
                await child.communicate()

    async def scenario():
        async with environment(core, tmp_path,
            purposes=("chat", "memory", "history", "goals", "exploration", "expression"),
            core_options={"operator_hot_reload": hot_reload, "goals_enabled": True,
                          "exploration_enabled": True, "mood_enabled": True,
                          # Keep the latest related turn source-bound while leaving
                          # older turns as ordinary history to exercise pruning.
                          "turn_recall_limit": 1,
                          "watch_ledger": core.settings.watch_ledger.model_copy(
                              update={"enabled": True})},
            history_bytes=64000) as (entry, sends):
            url = "https://www.bilibili.com/video/BV1xx411c7mD"
            event = await entry.admit(1, "你刚才" + fact + "，看看这个 " + url)
            goal = core.goals.propose(GoalProposal(topic=ASTRONOMY,
                source_event_ids=[event["event_id"]],
                scope=GoalScope(operation="read_content", url=url)))
            core.goals.accept(goal)
            step = core.goals.claim()
            source = result_for(core, goal, source="bilibili")
            core.goals.finish(step, source)
            judge(core, source, -1, fact)
            mid = core.memory.add(Candidate(subject="self", kind="fact", fact_key="star-view",
                content=fact,
                evidence_event_id=event["event_id"], evidence_quote="你刚才" + fact),
                core.clock(), confirmed=True)
            assert mid
            await worker("read")
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            core.clock.advance(1)
            await entry.admit(2, "这个回答很有帮助")
            await worker("feedback")
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            # These delivered old turns create genuine budget pressure.
            for number in range(3, 5):
                core.clock.advance(1)
                await entry.admit(number, "烘焙闲聊" * 900)
                await worker("background-" + str(number))
                assert (await entry.send(entry.client, entry.protocol))["worked"]
            core.clock.advance(1)
            await entry.admit(5, "天文光谱标注那个疑问还没解释清楚")
            await worker("important-turn")
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            core.clock.advance(1)
            event = await entry.admit(6, "继续说说天文光谱，你当时的判断是什么？")
            messages = await worker("selected")
            # Transcript/date source guidance is added only for turns carrying such
            # material; this ordinary recall turn keeps the base prompt size.
            assert messages[0]["role"] == "system"
            assert SOURCE_RULES not in messages[0]["content"]
            prompt = "\n".join(m["content"] for m in messages)
            # Exclude the raw original owner utterance: reference copies occur once.
            refs = [m for m in messages if m["role"] == "user" and any(
                heading in m.get("content", "") for heading in (
                    "目标与结果参考", "你最近看过的内容", "你记得的事（", "自身兴趣与过往判断"))]
            assert encode(refs).count(fact) == 1
            assert "主人发给你后你决定去看的" in prompt
            assert "光谱标注那个疑问还没解释清楚" in prompt
            assert '"negative"' in prompt
            assert "小肥鱼的心情参考" not in prompt
            job = core.store.one('SELECT job_id FROM jobs WHERE event_id=%s', (event["event_id"],))
            frozen = json.loads(core.store.one(
                'SELECT snapshots_json FROM agent_conversations WHERE job_id=%s', (job["job_id"],)
            )["snapshots_json"])
            plan = next(p["metadata"]["core_context"] for p in frozen
                        if p["kind"] == "context_plan")
            dropped = [*plan["assembly_dropped"], *plan["requests"][-1]["dropped"]]
            assert any(item["priority"] == 0 for item in dropped)
            assert any(item["kind"] == "history" for item in dropped)
            assert (await entry.send(entry.client, entry.protocol))["worked"]
            assert len(sends) == 6
            assert core.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == len(calls)

    run(scenario())


def test_source_rules_follow_only_transcript_or_dated_source_material():
    from companion.model import needs_source_rules

    assert not needs_source_rules('{"role":"user","content":"天文光谱"}')
    for marker in ('"transcripts"', '"attachments_read"', '"prior_assistant_interpretation"',
                   '"source_message_on"'):
        assert needs_source_rules('{"x":1,' + marker + ':[]}')
