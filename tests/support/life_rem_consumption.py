from contextlib import asynccontextmanager

import httpx
from pydantic import SecretStr

from companion import owner_authority, paid_budget
from companion.life_protocol import LifeLoopConfig, LifeShadowApproval
from companion.model import ModelClient
from companion.owner_authority_protocol import AuthorityStop, OwnerGrantPolicy
from companion.rem_protocol import RemConfig
from companion.store import after
from tests.support.life_expression_entrypoint import native_child
from tests.support.owner_authority import activate
from tests.support.owner_memory import environment
from tests.support.rem_native import response


@asynccontextmanager
async def prepared(core, tmp_path, *, automatic=False, purposes=("rem", "memory"),
                   creation=True, enabled=True, capture_key=None):
    core.settings.rem = RemConfig(enabled=enabled, local_hour=0,
                                  daily_micro_usd=50000, monthly_micro_usd=200000)
    core.settings.life_loop = LifeLoopConfig(mode="SHADOW", follow_up_delay_seconds=60)
    core.settings.wake_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    core.settings.control_token = SecretStr("synthetic-life-rem-control")
    async with environment(core, tmp_path, history=False) as (admit, recall, original):
        capture, _ = await admit(11, "我想把夜空和纸上的花园写成一个小故事。")
        policy = owner_authority.current(core.store, core.settings, core.clock).policy
        # Native STOP cancels pending foreground custody before this explicit new scope.
        stopped = owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id="stop-seed-foreground", authority=original))
        conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                      (capture.event_id,))["conversation_id"]
        scope = LifeShadowApproval(
            scope="owner_private_life_shadow_v1", authorization_sha256="c" * 64,
            conversation_id=conversation, source_event_ids=[capture.event_id],
            purposes=["PRIVATE_CREATION"] if creation else ["PRIVATE_DIARY"],
            expires_at=after(core.clock(), 86400), max_items=3,
            automatic_actions=["rem.save"] if automatic else [],
        )
        policy = OwnerGrantPolicy.model_validate({**policy.model_dump(),
            "purposes": ["chat", "life", *purposes], "life_shadow": scope,
            "capture": {**policy.capture.model_dump(),
                        "public_key": capture_key.public_key().public_bytes_raw().hex()}
            if capture_key else policy.capture,
            "memory_scope": "owner_private" if "memory" in purposes else "none",
            "capability_policies": {"rem": paid_budget.sha(core.settings.rem.model_dump())}
            if "rem" in purposes else {}})
        reference = activate(core, policy, revision=stopped["authority"]["revision"],
                             operation="enable-life-private-creation")[0]["authority"]
        core.clock.advance(max(61, core.settings.proactive_idle_seconds + 1))
        yield reference, capture, recall, policy


async def draft(core, tmp_path, capture, *, independent=False):
    if independent:
        outcome = await native_child(core, tmp_path, capture.event_id, "PRIVATE_CREATION", "draft")
        assert outcome["provider_calls"] == 1, outcome
        rows = [row for row in outcome["results"] if row["state"] == "SHADOW"]
        assert len(rows) == 1, outcome
        return rows[0]
    job_id = core.life_topics.schedule()
    assert job_id
    value = {"action": "SHARE", "destination": "PRIVATE_CREATION",
             "source_event_ids": [capture.event_id], "reason": "私人虚构创作",
             "text": "虚构：夜空化成了纸上的花园。"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response(value)),
    ) as client:
        previous = core.model
        core.model = ModelClient(core.settings, client)
        try:
            assert await core.run_once(job_id=job_id)
        finally:
            core.model = previous
    row = core.store.one("SELECT * FROM life_activity_results WHERE job_id=?", (job_id,))
    assert row["state"] == "SHADOW", core.store.rows("SELECT * FROM jobs")
    return row
