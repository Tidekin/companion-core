"""Required task feedback reaches native Bridge with checked source and ordered ACKs."""

import json
from datetime import timedelta

import httpx
import pytest

from companion import notices, owner_delivery
from companion.config import ObservationAdapterConfig
from companion.core import Core
from companion.goals import CommitmentRequest, GoalProposal, GoalScope
from companion.model import ModelClient
from companion.reply_segments import SegmentedReplyConfig
from companion_onebot_bridge.outbound import validate_item
from tests.conftest import run
from tests.native_continuous_support import frame, host
from tests.support.exploration import URL
from tests.support.onebot_events import config
from tests.support.private_companion import companion_options


@pytest.mark.parametrize("outcome", ["FULFILLED", "EXPLAINED_FAILURE"])
def test_required_notice_native_reader_worker_bridge_and_complete_ack(core, tmp_path, outcome):
    async def source(ws):
        await ws.send(frame(1, text="请稍后帮我阅读这条资料"))
        await ws.wait_closed()

    async def scenario():
        options = companion_options(core)
        policy = SegmentedReplyConfig(max_segments="unlimited", interval_ms=0)
        options["core_options"].update(
            segmented_reply=policy, content_enabled=True, observation_enabled=True,
            observation_adapters={"bili": ObservationAdapterConfig(
                source="bilibili", token="synthetic-notice-reader-token-12345")},
            exploration_daily_requests=8, exploration_monthly_requests=30,
            exploration_daily_bytes=4_194_304,
        )
        async with host(core, tmp_path, source, policy=policy,
            purposes=("chat", "history", "memory", "goals", "proactive", "content", "exploration"),
            core_options=options["core_options"], paid_options={"per_request_calls": 3},
            bridge_options={"rich_input_scope": "references_v1"}) as (window, _, sends, _):
            client = window.client
            calls, leased = [], []

            async def remember_lease(response):
                if response.request.url.path == "/internal/v1/deliveries/lease":
                    await response.aread()
                    leased.extend(response.json()["items"])

            client.event_hooks["response"].append(remember_lease)

            def provider(request):
                payload = json.loads(request.content)
                is_notice = '"kind":"commitment"' in payload["messages"][-1]["content"]
                calls.append("notice" if is_notice else "chat")
                text = "我记下了。"
                if is_notice:
                    text = ("资料读到了，我们可以聊聊其中的手绘画面。" if outcome == "FULFILLED"
                            else "这次没能在约定时间内读完资料。")
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": text}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                })

            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model_http:
                core.model = ModelClient(core.settings, model_http)
                await window.capture()
                await window.flush()
                assert await core.run_once()
                await window.pump()
                owner = core.store.one("SELECT event_id FROM events WHERE actor_id='owner'")[
                    "event_id"]
                goal = core.goals.accept_commitment(CommitmentRequest(
                    proposal=GoalProposal(topic="阅读公开资料", source_event_ids=[owner],
                        scope=GoalScope(operation="read_content", url=URL)),
                    accepted_event_id=owner, deadline=core.clock.now + timedelta(seconds=60),
                ))
                if outcome == "FULFILLED":
                    assert core.exploration.schedule_goal()
                    headers = {"Authorization": "Bearer synthetic-notice-reader-token-12345"}
                    response = await client.post("/internal/v2/readers/bili/lease", headers=headers)
                    assert response.status_code == 200
                    item = response.json()["item"]
                    response = await client.post(
                        "/internal/v2/readers/bili/" + item["request_id"] + "/result",
                        headers=headers, json={"lease_token": item["lease_token"],
                            "status": "SUCCESS", "used_bytes": 1800,
                            "items": [{"title": "手绘游戏", "text": "简介：游戏采用手绘画面。",
                                       "url": URL}]},
                    )
                    assert response.status_code == 200, response.text
                    assert core.goals.get(goal["goal_id"])["state"] == "DONE"
                else:
                    core.clock.advance(61)
                    assert core.goals.claim() is None
                core.goals.pause_autonomy(True)
                with Core(core.settings, core.model, core.clock) as worker:
                    assert worker.store is not core.store
                    assert await worker.run_once("required-notice", job_id=core.store.one(
                        "SELECT job_id FROM jobs WHERE kind='activity'")["job_id"])
                assert calls == ["chat", "notice"]
                assert core.store.one("SELECT state FROM jobs WHERE kind='activity'") == {
                    "state": "SUCCEEDED"}
                assert core.store.one("SELECT state FROM commitments")["state"] == "IN_PROGRESS"
                await window.pump()
                if outcome == "FULFILLED":
                    assert len(sends) == 2
                    assert core.store.one("SELECT state FROM commitments")["state"] == "IN_PROGRESS"
                    await window.pump()
                    assert sends[-1]["message"] == [{"type": "text", "data": {"text": URL}}]
                assert len(sends) == (3 if outcome == "FULFILLED" else 2)
                assert core.store.one("SELECT state FROM commitments")["state"] == outcome
                for index, item in enumerate(leased):
                    assert item["authority"] == window.runtime.authority
                    material = json.loads(core.store.one(
                        'SELECT value FROM runtime_meta WHERE key=%s',
                        (owner_delivery.DELIVERY + item["message_id"],))["value"])
                    assert notices.required(core.store, material["job_id"]) is (index > 0)
                assert window.runtime.row()["state"] == "ACTIVE"

    run(scenario())


@pytest.mark.parametrize("companion,marker,action", [
    (False, "commitment", "qq.private.reply"),
    (True, "share", "qq.private.reply"),
    (True, None, "qq.private.reply"),
    (True, "commitment", "qq.private.proactive"),
])
def test_notice_lease_marker_is_restricted_to_required_companion_reply(companion, marker, action):
    cfg = config()
    item = {"message_id": "msg_" + "a" * 32, "lease_token": "lease_" + "b" * 32,
            "lease_until": "2026-10-23T09:00:00+00:00", "window_notice": marker,
            "conversation_platform_session_key": cfg.origin, "action_type": action,
            "content": {"text": "任务反馈"}}
    with pytest.raises(ValueError, match="INVALID_NOTICE_SCOPE"):
        validate_item(item, cfg, companion=companion)
