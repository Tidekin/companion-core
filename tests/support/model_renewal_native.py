from datetime import timedelta
from types import SimpleNamespace

from pydantic import SecretStr

from companion.life_protocol import LifeLoopConfig, LifeShadowApproval
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.store import after
from companion_onebot_bridge.config import BridgeConfig
from tests import owner_model_fixtures
from tests.model_carry_support import migrated
from tests.support.model_renewal import renewal
from tests.support.reading_followup import admit


def life_case(core, tmp_path, monkeypatch):
    def approved_policy(**values):
        return OwnerGrantPolicy(
            **{
                **values,
                "purposes": ["chat", "proactive", "history"],
                "history": {
                    "scope": "owner_private",
                    "not_before": after(core.clock(), -3600),
                    "max_turns": 16,
                    "max_utf8_bytes": 64000,
                },
            }
        )

    with monkeypatch.context() as patch:
        patch.setattr(owner_model_fixtures, "OwnerGrantPolicy", approved_policy)
        case = migrated(core, tmp_path, expires_at=core.clock.now + timedelta(hours=2),
                        rates_valid_until=core.clock.now + timedelta(days=2))
    core.settings.control_token = SecretStr("synthetic-renewal-control")
    core.settings.life_loop = LifeLoopConfig(mode="SHADOW", follow_up_delay_seconds=60)
    core.settings.wake_enabled = True
    core.settings.proactive_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    core.clock.now = core.settings.paid_model.expires_at - timedelta(hours=2)
    return case


async def source_and_renewal(core, tmp_path, case, http):
    config = BridgeConfig(
        owner_qq_id=core.settings.owner_qq_id,
        bot_qq_id=core.settings.bot_qq_id,
        bridge_token=core.settings.bridge_token.get_secret_value(),
        onebot_token="s" * 32,
        protocol_epoch=case.policy.capture.protocol_epoch,
    )
    plan = SimpleNamespace(authority=case.successor["authority"], key=case.key, policy=case.policy)
    incoming, _ = await admit(http, core, config, plan, 2, "导出Bug还没解决，明天接着看")
    source_id = incoming["event_id"]
    request, policy, witness = renewal(core, case)
    scope = LifeShadowApproval(
        scope="owner_private_life_shadow_v1",
        authorization_sha256=policy.authorization_sha256,
        conversation_id=core.store.one(
            "SELECT conversation_id FROM events WHERE event_id=?", (source_id,)
        )["conversation_id"],
        source_event_ids=[source_id],
        purposes=["PRIVATE_MESSAGE_DRAFT"],
        automatic_actions=["qq.private.proactive"],
        expires_at=policy.expires_at,
        max_items=1,
    )
    policy = OwnerGrantPolicy.model_validate(
        {**policy.model_dump(), "purposes": [*policy.purposes, "life"], "life_shadow": scope}
    )
    request["successor_policy"] = policy.model_dump(mode="json")
    return source_id, request, policy, witness
