"""Synthetic input/call-chain checks, not ratings of generated conversation quality."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from companion import owner_input
from companion.conversation_policy import PARTICIPATION_V2, ConversationPolicyConfig
from companion.reply_segments import SEMANTIC_RULES, SegmentedReplyConfig
from scripts.evaluate_r1_conversation import load_cases
from tests.conftest import run
from tests.support.dialogue_context import current_job, sent_history
from tests.support.native_consumer_entry import environment
from tests.support.native_history_delivery import sdk_step
from tests.support.token_measurement import configure

CASES = load_cases(Path(__file__).parent / "fixtures/conversation_turn_cases.json")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_same_chat_call_receives_the_actual_turns_and_times(core, case):
    core.settings.conversation_policy = ConversationPolicyConfig()
    core.settings.chat_time_labels = True
    core.settings.context_soft_tokens = 16000
    core.settings.max_recent_messages = 30
    history = case["inputs"]["history"]
    for number, turn in enumerate(history, 1):
        message = sent_history(core, number, turn["user"], turn["assistant"])
        core.store.execute('UPDATE outbox SET sent_at=%s WHERE message_id=%s',
                           (core.clock(), message))
        if number < len(history):
            core.clock.advance(30)
    core.clock.advance(case["metadata"]["gap_seconds"])
    job = current_job(core, text=case["inputs"]["text"])
    assert run(core.run_once())
    [(messages, scenario)] = core.model.calls
    assert scenario == "chat" and PARTICIPATION_V2 in messages[0]["content"]
    # No keyword classifier substitutes for the delivered conversation or drops it.
    turns = messages[1:-2]
    assert len(turns) == 2 * len(history)
    for number, turn in enumerate(history):
        owner, reply = turns[number * 2:number * 2 + 2]
        assert owner["role"] == "user" and owner["content"].endswith(turn["user"])
        assert owner["content"].startswith("【10月19日 周一 12:")
        assert reply == {"role": "assistant", "content": turn["assistant"]}
        assert turn["user"] not in messages[0]["content"]
    day = "22" if case["metadata"]["gap_seconds"] == 259200 else "19"
    assert f"2026年10月{day}日" in messages[-2]["content"]
    assert messages[-1] == {"role": "user", "content": case["inputs"]["text"]}
    # All cases, including short requests, use one ordinary reply job. No new
    # activity job, timer, additional model call or proactive delivery is created.
    assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (job["job_id"],)) == {
        "state": "SUCCEEDED"}
    assert core.store.one("SELECT COUNT(*) n FROM jobs")["n"] == len(history) + 1
    assert core.store.rows("SELECT DISTINCT action_type FROM outbox") == [
        {"action_type": "qq.private.reply"}]
    assert core.store.one("SELECT COUNT(*) n FROM model_usage WHERE state='SETTLED'")["n"] == 1


def test_comparison_cases_distinguish_contexts_and_include_behavior_rubrics():
    assert {case["metadata"]["category"] for case in CASES} >= {
        "lull", "short_reply", "sharing", "disclosure", "joke", "disagreement",
        "multi_turn", "help", "preclosing", "closing", "long_gap", "external_text"}
    acknowledgements = [case for case in CASES if case["inputs"]["text"] == "嗯嗯"]
    assert len(acknowledgements) == 2
    assert acknowledgements[0]["inputs"]["history"] != acknowledgements[1]["inputs"]["history"]
    assert all(case["metadata"]["rubric"] and "expected_output" not in case for case in CASES)


def test_native_entry_and_independent_worker_keep_turn_sources_and_delivery(core, tmp_path):
    # Exercise the existing verified local tokenizer at the default soft limit;
    # only provider HTTP is mocked, including in each independent SDK worker.
    configure(core, tmp_path, policy="exact_required")

    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "history"),
            core_options={"conversation_policy": ConversationPolicyConfig(),
                          "chat_time_labels": True,
                          "segmented_reply": SegmentedReplyConfig(mode="semantic")},
            paid_options={"provider": core.settings.provider, "model": core.settings.model_name,
                          "endpoint": core.settings.model_url}, expires_in=60) as (entry, sends):
            events = []
            for number, text in enumerate(("猫把纸箱挤塌了", "你来说点什么", "怎么弄"), 1):
                source = await entry.admit(number, text)
                job = core.store.one((
                    'SELECT j.*,e.body_json,e.conversation_id FROM jobs j '
                    'JOIN events e USING(event_id) WHERE e.event_id=%s'
                ),
                                     (source["event_id"],))
                eligible = owner_input.history_sources(core.store, core.settings, core.clock, job)
                assert len(eligible) == number - 1
                job_id, wire = await sdk_step(core, tmp_path, source["event_id"], number)
                assert PARTICIPATION_V2 in wire["messages"][0]["content"]
                assert SEMANTIC_RULES in wire["messages"][0]["content"]
                assert "不分析、不解释自己" not in wire["messages"][0]["content"]
                answers = sum(item["role"] == "assistant" for item in wire["messages"])
                assert answers == number - 1, {
                    "system_bytes": len(wire["messages"][0]["content"].encode()),
                    "soft_tokens": core.settings.context_soft_tokens,
                }
                for previous in events:
                    assert previous["payload"]["text"] in json.dumps(
                        wire["messages"], ensure_ascii=False)
                row = core.store.one((
                    'SELECT snapshots_json FROM agent_conversations WHERE '
                    'job_id=%s'
                ), (job_id,))
                snapshots = json.loads(row["snapshots_json"])
                history = next(item["proof"] for item in snapshots if item["kind"] == "history")
                assert [item["event_id"] for item in history] == [
                    event["event_id"] for event in events]
                policy = next(item["proof"] for item in snapshots
                              if item["kind"] == "conversation_policy")
                assert policy["strategy"] == "participation_v2"
                assert (await entry.send(entry.client, entry.protocol))["worked"]
                delivered = core.store.one('SELECT sent_at FROM outbox WHERE origin_event_id=%s',
                                            (source["event_id"],))
                # Bridge send receipts use wall time. Subprocess startup can outlast
                # the synthetic tick; the next input must follow actual delivery.
                core.clock.now = max(core.clock.now, datetime.fromisoformat(delivered["sent_at"]))
                events.append(source)
                core.clock.advance(1)
            assert len(sends) == 3
            assert core.store.one("SELECT COUNT(*) n FROM model_usage "
                                  "WHERE state='SETTLED'")["n"] == 3
            assert not core.store.one("SELECT 1 FROM jobs WHERE kind!='chat'")
            assert not core.store.one("SELECT 1 FROM outbox WHERE action_type!='qq.private.reply'")
    run(scenario())
