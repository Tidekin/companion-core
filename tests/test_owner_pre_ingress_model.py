"""A signed capture arriving in the final HTTP hook must preempt background I/O."""

import asyncio
import hashlib
import json

import httpx

from companion import owner_authority, owner_input
from companion.life_protocol import LifeLoopConfig, LifeShadowApproval
from companion.model import ModelClient
from companion.owner_authority_protocol import OwnerCapture, OwnerGrantPolicy
from companion.store import after
from companion_delivery_protocol.authority import capture_signing_payload
from tests.support.owner_authority import activate
from tests.support.owner_input import captured, inbound


def test_signed_pre_ingress_arriving_in_final_hook_preempts_life_provider(core, tmp_path):
    core.settings.life_loop = LifeLoopConfig(mode="SHADOW")
    core.settings.wake_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    capture, key = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    core.ingest(inbound(core, capture), "preempt-source-input")
    core.store.execute("UPDATE jobs SET state='CANCELLED',lease_token=NULL")
    policy = owner_authority.current(core.store, core.settings, core.clock).policy
    conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                  (capture.event_id,))["conversation_id"]
    shadow = LifeShadowApproval(
        scope="owner_private_life_shadow_v1", authorization_sha256="d" * 64,
        conversation_id=conversation, source_event_ids=[capture.event_id],
        purposes=["PRIVATE_MESSAGE_DRAFT"], expires_at=after(core.clock(), 3 * 86400), max_items=2,
    )
    policy = OwnerGrantPolicy.model_validate({**policy.model_dump(), "purposes": ["chat", "life"],
                                              "life_shadow": shadow.model_dump()})
    active, _ = activate(core, policy, revision=1, operation="life-preemption-grant")
    core.clock.advance(86400)
    job_id = core.life_topics.schedule()
    assert job_id
    value = capture.model_dump(mode="json", exclude={"signature"})
    source = capture.source_event_id.rsplit(":", 1)[0] + ":124"
    value.update(authority=active["authority"], source_event_id=source,
                 event_id="evt_" + hashlib.sha256(source.encode()).hexdigest(),
                 occurred_at=core.clock(), received_at=core.clock())
    value["signature"] = key.sign(capture_signing_payload(value)).hex()
    arriving = OwnerCapture.model_validate(value)
    calls = []

    async def hook(request):
        owner_input.approve(core.store, core.settings, core.clock, arriving)

    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "action": "SKIP", "destination": "NONE", "source_event_ids": [],
                "reason": "No new observation",
            })}}], "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider), trust_env=False,
                                     event_hooks={"request": [hook]}) as client:
            core.model = ModelClient(core.settings, client)
            assert await core.run_once(job_id=job_id)

    asyncio.run(execute())
    assert core.store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                          (owner_input.INPUT + arriving.event_id,)), core.store.rows(
        "SELECT state,last_error_code FROM jobs WHERE job_id=?", (job_id,),
    )
    assert not core.store.one("SELECT 1 FROM events WHERE event_id=?", (arriving.event_id,))
    assert calls == [], "An approved owner capture must preempt before external background I/O"
    assert core.store.one(
        "SELECT state,input_tokens,output_tokens,billed_micro_usd,is_estimate "
        "FROM model_usage WHERE job_id=?", (job_id,),
    ) == {"state": "SETTLED", "input_tokens": 0, "output_tokens": 0,
          "billed_micro_usd": 0, "is_estimate": 0}
    assert core.store.rows("SELECT * FROM outbox") == []
