"""Recent delivered dialogue wins the native history allowance over old Hot episodes."""

import json
from datetime import datetime

import httpx
import pytest

from companion import dialogue_history, owner_input
from companion.goals import GoalProposal
from companion.store import encode
from tests.conftest import run
from tests.sdk_reader_fixture import answer, worker_for
from tests.support.native_consumer_entry import environment
from tests.support.native_history_delivery import sdk_step


@pytest.mark.parametrize("turns,independent,history_bytes,padding,expected_items", [
    (10, False, 16000, 0, 1), (15, True, 16000, 0, 1), (3, False, 3500, 500, 0),
])
def test_native_recent_turns_precede_fifteen_hot_sources(
    core, tmp_path, turns, independent, history_bytes, padding, expected_items,
):
    async def scenario():
        async with environment(core, tmp_path, purposes=("chat", "memory", "history", "goals"),
            # Raw turn recall shares this ceiling first; it is off here so the test keeps
            # isolating recent turns against episode continuation (see test_turn_recall).
            core_options={"goals_enabled": True, "context_soft_tokens": 16000,
                          "turn_recall_limit": 0,
                          "max_recent_messages": 20, "chat_thinking": "high",
                          "chat_thinking_tokens": 3072, "max_output_tokens": 2048},
            paid_options={"daily_calls": 30, "provider": "deepseek", "model": "deepseek-flash",
                          "max_output_tokens": 5120},
            history_bytes=history_bytes) as (entry, sends):
            old_sources = []
            for group in range(3):
                events = []
                for offset in range(5):
                    source = await entry.admit(group * 5 + offset + 1,
                        f"old telescope project {group} observation {offset}")
                    # The old unsent sources form genuine episodes, not delivered turns.
                    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE event_id=%s",
                                       (source["event_id"],))
                    events.append(core.store.one('SELECT * FROM events WHERE event_id=%s',
                                                 (source["event_id"],)))
                    core.clock.advance(1)
                core.memory.episodes(events, core.clock())
                goal = core.goals.propose(GoalProposal(topic=f"telescope project {group}",
                    source_event_ids=[event["event_id"] for event in events]))
                core.goals.accept(goal)
                old_sources.extend(event["event_id"] for event in events)
            core.clock.advance(8 * 86400)
            calls = []

            def provider(request):
                calls.append(json.loads(request.content))
                return httpx.Response(200, json=answer(
                    "r" * padding + f"native delivered reply {len(calls)}"))

            recent = []
            async with worker_for(core, provider) as worker:
                for number in range(1, turns + 1):
                    source = await entry.admit(100 + number,
                        "u" * padding + f"recent ordinary turn {number}")
                    job_id = entry.accepted[source["event_id"]]["job_id"]
                    assert await worker.run_once("recent-history", job_id=job_id)
                    assert (await entry.send(entry.client, entry.protocol))["worked"]
                    sent = core.store.one('SELECT sent_at FROM outbox WHERE origin_event_id=%s',
                                           (source["event_id"],))
                    core.clock.now = max(core.clock.now, datetime.fromisoformat(sent["sent_at"]))
                    core.clock.advance(1)
                    recent.append(source["event_id"])

                current = await entry.admit(999, "continue the recent ordinary conversation")
                job = core.store.one((
                    'SELECT j.*,e.conversation_id FROM jobs j JOIN events e '
                    'USING(event_id) WHERE event_id=%s'
                ), (current["event_id"],))
                eligible = owner_input.history_sources(core.store, core.settings, core.clock, job)
                selected_recent = recent[-10:]
                assert [source["event_id"] for source in eligible] == selected_recent
                candidates = core.memory_context.continuation(job["conversation_id"], core.clock(),
                    event_ids=old_sources, current_event_ids=selected_recent)["items"]
                assert len(candidates) == 3
                assert all(item["temperature"] == "Hot" for item in candidates)
                assert len({key for item in candidates for key in item["source_event_ids"]}) == 15

                if independent:
                    job_id, wire = await sdk_step(core, tmp_path, current["event_id"], 999)
                else:
                    assert await worker.run_once("recent-history-current", job_id=job["job_id"])
                    job_id, wire = job["job_id"], calls[-1]
                row = core.store.one((
                    'SELECT snapshots_json FROM agent_conversations WHERE '
                    'job_id=%s'
                ), (job_id,))
                snapshots = json.loads(row["snapshots_json"])
                history = next(item["proof"] for item in snapshots if item["kind"] == "history")
                assert [item["event_id"] for item in history] == selected_recent
                assert sum(message["role"] == "assistant" for message in wire["messages"]) == min(
                    turns, 10)
                for number in range(max(1, turns - 9), turns + 1):
                    assert any(message["role"] == "user" and message["content"].endswith(
                        f"recent ordinary turn {number}") for message in wire["messages"])
                    assert any(message["role"] == "assistant" and message["content"].endswith(
                        f"native delivered reply {number}") for message in wire["messages"])
                continuations = [message for message in wire["messages"]
                    if isinstance(message.get("content"), str)
                    and message["content"].startswith("相关情景接续")]
                items = [item for message in continuations
                         for item in json.loads(message["content"].split("\n", 1)[1])]
                assert len(items) == expected_items
                selected = {key for item in items for key in item["source_event_ids"]}
                assert len(selected | set(selected_recent)) <= 15
                selected_history_bytes = sum(len(message["content"].encode()) for source in eligible
                    for message in dialogue_history.turn_messages(source))
                assert selected_history_bytes + sum(len(message["content"].encode())
                                                    for message in continuations) <= history_bytes
                assert wire["thinking"] == {"type": "enabled"}
                assert wire["reasoning_effort"] == "high"
                assert wire["max_tokens"] == 5120
                assert core.settings.chat_thinking_tokens == 3072
                assert (await entry.send(entry.client, entry.protocol))["worked"]
                assert len(sends) == turns + 1
                usage = core.store.rows(
                    'SELECT scenario,state FROM model_usage ORDER BY '
                    'record_sequence'
                )
                assert usage == [{"scenario": "chat", "state": "SETTLED"}] * (turns + 1)
                assert not core.store.one("SELECT 1 FROM agent_tool_calls")
                assert "old telescope" not in encode([message for message in wire["messages"]
                    if message["role"] == "assistant"])
    run(scenario())
