"""A verified owner frame outranks an admitted Life draft before transport dispatch."""

import json
from dataclasses import replace
from datetime import timedelta

from companion import owner_authority
from companion.core import Core
from companion.life_protocol import LifeLoopConfig, LifeShadowApproval
from companion.model import EXTRACT_RULES, RECALL_RULES, REVIEW_RULES
from companion.owner_authority_protocol import AuthorityStop
from companion_onebot_bridge import owner_handoff
from companion_onebot_bridge.onebot_client import OneBotClient
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.native_continuous_support import frame, host
from tests.test_input_interrupt_bridge import model_response
from tests.test_owner_authority import activate
from tests.test_private_companion import companion_options


def test_queued_owner_input_cancels_proactive_before_dispatch_without_unknown(core, tmp_path):
    cancel_requests, sources = [], []
    number = 0

    async def source(ws):
        nonlocal number
        number += 1
        await ws.send(frame(number,
            text="我喜欢龙井茶" if number == 1 else "等等，先聊我的事情"))
        await ws.wait_closed()

    def answer(request):
        messages = json.loads(request.content)["messages"]
        if messages[0]["content"] == EXTRACT_RULES:
            event_id = json.loads(messages[-1]["content"])["events"][0]["ref"]
            text = json.dumps({"memories": [{
                "subject": "user", "kind": "preference", "fact_key": "drink",
                "content": "用户喜欢龙井茶", "evidence_event_id": event_id,
                "evidence_quote": "我喜欢龙井茶",
            }]}, ensure_ascii=False)
        elif messages[0]["content"] == REVIEW_RULES:
            data = json.loads(messages[-1]["content"])
            text = json.dumps({"relations": [{"supported": True, "update_authorized": False,
                "claims_replacement": False,
                "same": None,
                "conflicts": [], "replaces": []} for _ in data["candidates"]]})
        elif messages[0]["content"] == RECALL_RULES:
            text = '{"relevant":["m0"]}'
        elif sources:
            text = json.dumps({"action": "SHARE", "destination": "PRIVATE_MESSAGE_DRAFT",
                "source_event_ids": sources, "reason": "聊聊已确认的饮茶偏好",
                "text": "我们聊聊龙井茶。"}, ensure_ascii=False)
        else:
            text = "我们聊聊龙井茶。"
        return model_response(text)

    async def scenario():
        async with host(core, tmp_path, source, model_reply=answer,
            purposes=("chat", "history", "memory", "proactive"),
            core_options=companion_options(core)["core_options"],
            paid_options={"per_request_calls": 3}) as (window, _, sends, _):
            await window.capture()
            await window.flush()
            assert await core.run_once()
            assert await window.pump()
            assert len(sends) == 1 and await core.extract_once()
            event = core.store.one(
                "SELECT event_id,conversation_id FROM events WHERE actor_id='owner'")
            sources.append(event["event_id"])
            scope = LifeShadowApproval(scope="owner_private_life_shadow_v1",
                authorization_sha256="c" * 64, conversation_id=event["conversation_id"],
                source_event_ids=sources, purposes=["PRIVATE_MESSAGE_DRAFT"], max_items=1,
                expires_at=core.clock.now + timedelta(days=2),
                automatic_actions=["qq.private.proactive"])
            original = owner_authority.current(core.store, core.settings, core.clock)
            window.runtime.stop()
            owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                authority=original.authority.as_dict(), operation_id="life-input-priority-renew"))
            window.spool.close()
            window.spool = Spool(window.config, reconciliation_only=True)
            core.clock.advance(1)
            window.runtime = OwnerRuntime(window.spool, clock=lambda: core.clock.now)
            assert (await owner_handoff.prepare(window.runtime, window.client))["state"] == (
                "HANDOFF_PREPARED")
            core.settings.life_loop = LifeLoopConfig(mode="SHADOW", follow_up_delay_seconds=3600)
            policy = original.policy.model_copy(update={
                "purposes": [*original.policy.purposes, "life"], "life_shadow": scope})
            successor = activate(core, policy, revision=original.authority.revision + 1,
                                 operation="life-input-priority-grant")[0]["authority"]
            assert (await owner_handoff.handoff(window.runtime, window.client, successor))[
                "state"] == "ACTIVE"
            window.config = replace(window.config, owner_authority=successor)
            window.restart()
            window.onebot = OneBotClient(window.config, window.protocol)
            with Core(core.settings, core.model, core.clock) as restarted:
                restarted.clock.advance(3601)
                job_id = restarted.life_topics.schedule()
                assert job_id
                assert await restarted.run_once("life-input-priority", job_id=job_id)
                assert restarted.life_topics.consume_due(diary=restarted.diary)
            assert core.store.one(
                "SELECT state FROM outbox WHERE action_type='qq.private.proactive'",
            )["state"] == "PENDING"
            usage = core.store.rows("SELECT * FROM model_usage")

            async def before(request):
                if request.url.path == "/send_private_msg":
                    await window.capture(2)

            async def cancellation(request):
                if request.url.path.endswith("/cancel-before-send"):
                    cancel_requests.append(json.loads(request.content))

            window.protocol.event_hooks["request"].append(before)
            window.client.event_hooks["request"].append(cancellation)
            assert await window.pump()
            assert len(sends) == 1 and window.runtime.row()["state"] == "ACTIVE"
            assert window.attempts() == {"ACK": 1, "NOT_SENT": 1}
            assert core.store.one(
                "SELECT state FROM outbox WHERE action_type='qq.private.proactive'",
            )["state"] == "CANCELLED"
            assert core.store.one("SELECT reply_revision FROM conversations")["reply_revision"] == 1
            assert len(cancel_requests) == 1
            assert cancel_requests[0]["cause"] == "OWNER_INPUT_PENDING"
            pending = window.db.execute("SELECT key FROM inbox WHERE state='pending'").fetchone()[0]
            assert cancel_requests[0]["pending_event_id"] == pending
            assert "reply_revision" not in cancel_requests[0]
            assert core.store.rows("SELECT * FROM model_usage") == usage
            window.runtime.verify(active=False)
    run(scenario())
