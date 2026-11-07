from datetime import timedelta

from companion import owner_authority
from companion.life_protocol import LifeLoopConfig, LifeShadowApproval
from companion.owner_authority_protocol import AuthorityStop, OwnerGrantPolicy
from tests.support.owner_authority import activate


def scope(core, conversation, ids=(), *, mode="owner_private_history"):
    return LifeShadowApproval(scope="owner_private_life_shadow_v1",
        authorization_sha256="e" * 64, conversation_id=conversation,
        source_scope=mode, source_event_ids=list(ids), purposes=["PRIVATE_MESSAGE_DRAFT"],
        expires_at=core.clock.now + timedelta(days=2), max_items=2)


def grant(core, reference, value, operation, *, history_bounds=None):
    view = owner_authority.current(core.store, core.settings, core.clock)
    stopped = owner_authority.stop(core.store, core.settings, core.clock,
        AuthorityStop(operation_id=operation+"-stop", authority=reference))
    policy = OwnerGrantPolicy.model_validate({**view.policy.model_dump(),
        "purposes": list(dict.fromkeys([*view.policy.purposes, "life"])),
        "history": {**view.policy.history.model_dump(), **(history_bounds or {})},
        "life_shadow": value})
    result, _ = activate(core, policy, revision=stopped["authority"]["revision"],
                         operation=operation+"-activate")
    return result["authority"], policy


def configure(core):
    core.settings.life_loop = LifeLoopConfig(mode="SHADOW", follow_up_delay_seconds=60,
        min_interval_seconds=60, autonomy_enabled=True, read_goals_enabled=True)
    core.settings.wake_enabled = True
    core.settings.memory_enabled = core.settings.diary_enabled = False
    core.settings.proactive_idle_seconds = 0
    core.settings.quiet_start = core.settings.quiet_end = 0
