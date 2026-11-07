from copy import deepcopy

from companion import live_text, paid_budget
from companion_onebot_bridge.window import WindowPlan, digest
from tests.support.cumulative_budget import policy


def bind_policy(core, plan, **changes):
    scope = policy(core, **changes)
    live = core.settings.live_text_window.model_copy(update={
        "cumulative": scope,
        "core_config_sha256": live_text.config_sha256(core.settings),
        "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings)),
    })
    value = deepcopy(plan.value)
    value["live_text"] = live.model_dump(mode="json", exclude={"plan_sha256"})
    inventory = {key: value[key] for key in (
        "identity_sha256", "protocol_epoch", "config_sha256", "config_version", "image_ids",
    )}
    authorization = None
    if value["execution"] == "approved_live":
        authorization = {"kind": "owner_qq_window", "approved": True,
            "scope_sha256": digest({k: v for k, v in value.items() if k != "authorization_sha256"}),
            "evidence_sha256": "a" * 64}
        value["authorization_sha256"] = digest(authorization)
    core.settings.live_text_window = live.model_copy(update={"plan_sha256": digest(value)})
    return WindowPlan(value, plan.config, inventory, authorization)
