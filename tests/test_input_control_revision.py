"""Signed owner controls survive reply supersession, never source revocation."""

import asyncio
import json

import httpx
import pytest

from companion import owner_authority
from companion.core import Core
from companion.model import REVIEW_RULES, ModelClient
from tests.conftest import FakeModel, run
from tests.native_input_protocol_fixture import notice
from tests.support.native_control_input import admit, environment
from tests.test_m1 import remember


def seed_memory(core):
    memory_id = remember(core, 99, "合成项目原定使用青色", confirmed=True)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.store.execute("UPDATE events SET memory_processed=1")
    return memory_id


@pytest.mark.parametrize("timing", ["queued", "running"])
def test_superseded_forget_executes_once_without_old_reply(core, tmp_path, monkeypatch, timing):
    target = seed_memory(core)

    async def scenario():
        async with environment(core, tmp_path, monkeypatch) as (client, config, plan, _):
            first, admitted = await admit(
                client, config, plan, core.clock.now, 1, "/忘记这一条 " + target)
            latest = None
            model = FakeModel()
            with Core(core.settings, model, core.clock) as worker:
                assert worker.store is not core.store
                original = worker.command_reply

                async def continue_before_action(job):
                    nonlocal latest
                    latest, _ = await admit(client, config, plan, core.clock.now, 2, "/发送状态")
                    return await original(job)

                if timing == "running":
                    monkeypatch.setattr(worker, "command_reply", continue_before_action)
                else:
                    latest, _ = await admit(client, config, plan, core.clock.now, 2, "/发送状态")
                    assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                                          (admitted["job_id"],))["state"] == "QUEUED"
                assert await worker.run_once("control-action")
                monkeypatch.setattr(worker, "command_reply", original)
                assert core.store.one("SELECT content,status FROM memories WHERE memory_id=?",
                                      (target,)) == {"content": "", "status": "FORGOTTEN"}
                assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                                      (admitted["job_id"],))["state"] == "SUCCEEDED"
                assert core.store.rows("SELECT * FROM outbox") == []
                ledger = core.ledger.path.read_bytes()
                duplicate = await client.post(
                    "/internal/v1/inbox/events", json=first,
                    headers={"X-Idempotency-Key": first["event_id"]},
                )
                assert duplicate.status_code == 202, duplicate.text
                assert duplicate.json()["duplicate"] and duplicate.json()["state"] == "SUCCEEDED"
                assert await worker.run_once("latest-reply")
                assert not await worker.run_once("no-replayed-action")
                assert core.ledger.path.read_bytes() == ledger
            assert core.store.rows("SELECT origin_event_id FROM outbox") == [
                {"origin_event_id": latest["event_id"]},
            ]
            assert model.calls == [] and core.store.rows("SELECT * FROM model_usage") == []
            assert owner_authority.status(
                core.store, core.settings, core.clock)["state"] == "ACTIVE"

    run(scenario())


def test_recalled_control_source_cancels_action_even_after_new_input(core, tmp_path, monkeypatch):
    target = seed_memory(core)

    async def scenario():
        async with environment(core, tmp_path, monkeypatch) as (client, config, plan, _):
            first, admitted = await admit(
                client, config, plan, core.clock.now, 1, "/忘记这一条 " + target)
            latest, _ = await admit(client, config, plan, core.clock.now, 2, "/发送状态")
            recalled = await client.post(
                "/internal/v1/owner-authority/inputs/recall",
                json=notice(config, plan, core.clock.now),
            )
            assert recalled.status_code == 200, recalled.text
            assert recalled.json()["state"] == "RECALLED"
            with Core(core.settings, FakeModel(), core.clock) as worker:
                assert await worker.run_once("latest-after-recall")
                assert not await worker.run_once("cancelled-control")
            assert core.store.one("SELECT status FROM memories WHERE memory_id=?",
                                  (target,))["status"] == "CONFIRMED"
            assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                                  (admitted["job_id"],))["state"] == "CANCELLED"
            assert core.store.rows("SELECT origin_event_id FROM outbox") == [
                {"origin_event_id": latest["event_id"]},
            ]
            duplicate = await client.post(
                "/internal/v1/inbox/events", json=first,
                headers={"X-Idempotency-Key": first["event_id"]},
            )
            assert duplicate.status_code == 410 and duplicate.json()["error"] == "EVENT_ERASED"
            assert owner_authority.status(
                core.store, core.settings, core.clock)["state"] == "ACTIVE"

    run(scenario())


@pytest.mark.parametrize("recall", [False, True])
def test_running_control_review_settles_before_action_or_source_cancellation(
    core, tmp_path, monkeypatch, recall,
):
    target = seed_memory(core)
    content = "合成项目新增的配色是浅绿"

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def provider(request):
            payload = json.loads(request.content)
            calls.append(payload)
            assert payload["messages"][0]["content"] == REVIEW_RULES
            entered.set()
            await release.wait()
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "relations": [{"supported": True, "update_authorized": False,
                        "claims_replacement": False, "same": None,
                                   "conflicts": [], "replaces": []}],
                })}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            })

        async with environment(core, tmp_path, monkeypatch) as (client, config, plan, _), (
            httpx.AsyncClient(transport=httpx.MockTransport(provider))
        ) as transport:
            first, admitted = await admit(
                client, config, plan, core.clock.now, 1, "/记住这个 " + content)
            with Core(core.settings, ModelClient(core.settings, transport), core.clock) as worker:
                existing = worker.store.one("SELECT * FROM memories WHERE memory_id=?", (target,))
                existing["evidence_ids"] = ["evt_test_99"]
                # Force the semantic-review branch; provider I/O and accounting remain real.
                async def existing_matches(*_, source_check, **__):
                    source_check()
                    return [existing]

                monkeypatch.setattr(worker.memory, "search_async", existing_matches)
                task = asyncio.create_task(worker.run_once("paid-control"))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    latest, _ = await admit(client, config, plan, core.clock.now, 2, "/发送状态")
                    if recall:
                        receipt = await client.post(
                            "/internal/v1/owner-authority/inputs/recall",
                            json=notice(config, plan, core.clock.now),
                        )
                        assert receipt.status_code == 200, receipt.text
                    release.set()
                    assert await asyncio.wait_for(task, 5)
                    assert await worker.run_once("new-control-reply")
                finally:
                    release.set()
                    await task
            saved = core.store.one("SELECT status FROM memories WHERE content=?", (content,))
            assert saved == (None if recall else {"status": "CONFIRMED"})
            assert core.store.one("SELECT state FROM jobs WHERE job_id=?", (admitted["job_id"],))[
                "state"] == ("CANCELLED" if recall else "SUCCEEDED")
            assert core.store.rows("SELECT origin_event_id FROM outbox") == [
                {"origin_event_id": latest["event_id"]},
            ]
            usage = core.store.rows("SELECT scenario,state,billed_micro_usd FROM model_usage")
            assert len(calls) == len(usage) == 1
            assert usage[0]["scenario"] == "memory_review" and usage[0]["state"] == "SETTLED"
            assert usage[0]["billed_micro_usd"] > 0
            assert not core.store.one("SELECT 1 FROM outbox WHERE origin_event_id=?",
                                      (first["event_id"],))
            assert owner_authority.status(
                core.store, core.settings, core.clock)["state"] == "ACTIVE"

    run(scenario())


def test_queued_diary_pause_resume_controls_keep_order_before_latest_reply(
    core, tmp_path, monkeypatch,
):
    core.settings.diary_enabled = True

    async def scenario():
        async with environment(core, tmp_path, monkeypatch) as (client, config, plan, _):
            paused, _ = await admit(client, config, plan, core.clock.now, 1, "/暂停写日记")
            resumed, _ = await admit(client, config, plan, core.clock.now, 2, "/恢复写日记")
            latest, _ = await admit(client, config, plan, core.clock.now, 3, "/发送状态")
            with Core(core.settings, FakeModel(), core.clock) as worker:
                assert await worker.run_once("pause")
                assert core.store.one("SELECT value FROM runtime_meta WHERE key='diary_paused'")[
                    "value"] == "1"
                assert await worker.run_once("resume")
                assert core.store.one("SELECT value FROM runtime_meta WHERE key='diary_paused'")[
                    "value"] == "0"
                assert await worker.run_once("status")
            for event in (paused, resumed):
                assert core.store.one("SELECT state FROM jobs WHERE event_id=?",
                                      (event["event_id"],))["state"] == "SUCCEEDED"
            assert core.store.rows("SELECT origin_event_id FROM outbox") == [
                {"origin_event_id": latest["event_id"]},
            ]
            assert owner_authority.status(
                core.store, core.settings, core.clock)["state"] == "ACTIVE"

    run(scenario())


def test_unknown_slash_input_keeps_ordinary_reply_supersession(core, tmp_path, monkeypatch):
    async def scenario():
        async with environment(core, tmp_path, monkeypatch) as (client, config, plan, _):
            first, _ = await admit(client, config, plan, core.clock.now, 1, "/普通聊天 想聊聊配色")
            latest, _ = await admit(client, config, plan, core.clock.now, 2, "/发送状态")
            with Core(core.settings, FakeModel(), core.clock) as worker:
                assert await worker.run_once("only-latest")
                assert not await worker.run_once("no-old-chat")
            assert core.store.one("SELECT state,last_error_code FROM jobs WHERE event_id=?",
                                  (first["event_id"],)) == {
                "state": "CANCELLED", "last_error_code": "REPLY_SUPERSEDED",
            }
            assert core.store.rows("SELECT origin_event_id FROM outbox") == [
                {"origin_event_id": latest["event_id"]},
            ]
            assert core.store.rows("SELECT * FROM model_usage") == []

    run(scenario())
