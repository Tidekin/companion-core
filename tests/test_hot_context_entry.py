"""Old unresolved attention uses spare history allowance through real HTTP/worker entry."""

import asyncio
import hashlib
import json
import sys
from datetime import datetime

import httpx
import pytest

from companion import owner_authority
from companion.goals import GoalProposal
from companion.owner_authority_protocol import OwnerCapture, OwnerGrantPolicy
from companion.qq_rich import input_digest
from companion.store import after
from companion_delivery_protocol.authority import capture_signing_payload
from tests.support.core_http import real_core_http
from tests.support.owner_authority import activate
from tests.test_evaluation_process import process_config
from tests.test_owner_input import captured, inbound


@pytest.mark.parametrize("recent_turns", [6, 9])
def test_hot_episode_uses_spare_history_capacity_and_cools_after_closure(core, tmp_path,
                                                                      recent_turns):
    original, key = captured(core, tmp_path)
    first_time = core.clock()
    core.settings.memory_enabled = core.settings.goals_enabled = True
    core.settings.context_soft_tokens = 12000
    core.settings.max_recent_messages = 16
    marker = "unresolved telescope project"

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as client:
            async def ingest(number, text, authority):
                source = original.source_event_id.rsplit(":", 1)[0] + ":" + str(number)
                value = original.model_dump(mode="json", exclude={"signature"}) | {
                    "authority": authority, "source_event_id": source,
                    "event_id": "evt_" + hashlib.sha256(source.encode()).hexdigest(),
                    "input_sha256": input_digest({"text": text}),
                    "received_at": core.clock(), "occurred_at": core.clock()}
                value["signature"] = key.sign(capture_signing_payload(value)).hex()
                capture = OwnerCapture.model_validate(value)
                response = await client.post("/internal/v1/owner-authority/inputs/approve",
                                             json=capture.model_dump(mode="json"))
                assert response.status_code == 200, response.text
                event = inbound(core, capture)
                event = event.model_copy(update={
                    "occurred_at": datetime.fromisoformat(core.clock()),
                    "payload": event.payload.model_copy(update={"text": text})})
                response = await client.post("/internal/v1/inbox/events",
                    json=event.model_dump(mode="json"),
                    headers={"X-Idempotency-Key": event.event_id})
                assert response.status_code == 202, response.text
                return event.event_id, response.json()["job_id"]

            old, old_job = await ingest(1, marker, original.authority)
            core.store.execute("UPDATE jobs SET state='CANCELLED' WHERE job_id=?", (old_job,))
            core.clock.advance(8 * 86400)
            view = owner_authority.current(core.store, core.settings, core.clock)
            policy = OwnerGrantPolicy.model_validate({**view.policy.model_dump(),
                "purposes": ["chat", "memory", "history", "goals"],
                "memory_scope": "owner_private", "history": {"scope": "owner_private",
                "not_before": after(first_time, -1), "max_turns": 8, "max_utf8_bytes": 8192}})
            authority = activate(core, policy, revision=1, operation="hot-context-history")[0][
                "authority"]
            event = core.store.one("SELECT * FROM events WHERE event_id=?", (old,))
            core.memory.episodes([event], core.clock())
            goal = core.goals.propose(GoalProposal(topic=marker, source_event_ids=[old]))
            core.goals.accept(goal)
            for number in range(2, recent_turns + 2):
                await ingest(number, f"recent ordinary turn {number}", authority)
                core.clock.advance(1)

            async def worker(number, expected_hot):
                _, job = await ingest(number, "continue our conversation", authority)
                config = tmp_path / f"settings-{number}.json"
                report = tmp_path / f"report-{number}.json"
                process_config(core, config)
                child = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
                    "tests.hot_context_worker", str(config), core.clock(), job, str(report),
                    str(min(recent_turns, 8)),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    _, error = await asyncio.wait_for(child.communicate(), 45)
                    assert child.returncode == 0, error.decode()
                finally:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()
                value = json.loads(report.read_text("utf-8"))
                messages = value["payload"]["messages"]
                continuation = [message for message in messages
                    if isinstance(message.get("content"), str)
                    and message["content"].startswith("相关情景接续")]
                assert (marker in json.dumps(continuation, ensure_ascii=False)) is expected_hot
                assert value["history_sources"] == value["available_history_sources"]
                if expected_hot:
                    assert old in value["sources"]
                    assert len(value["sources"]) <= 8
                history_bytes = sum(len(message["content"].encode()) for message in messages
                    if isinstance(message.get("content"), str) and (
                        message["content"].startswith("相关情景接续")
                        or message["content"].startswith("recent ordinary turn")))
                assert history_bytes <= 8192
                assert value["input_tokens"] <= core.settings.context_soft_tokens

            await worker(20, recent_turns < 8)
            core.goals.cancel(goal)
            core.clock.advance(1)
            assert not core.memory_context.continuation(
                event["conversation_id"], core.clock(), event_ids=[old])["items"]
            await worker(21, False)

    asyncio.run(scenario())
