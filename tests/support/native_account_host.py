from dataclasses import replace

from companion import paid_budget
from companion.owner_authority_protocol import OwnerGrantPolicy
from tests.support.onebot_events import config
from tests.support.owner_authority import activate


def configure_host(core, tmp_path, policy):
    cfg = replace(config(), data_dir=str(tmp_path / "bridge"), capture_signing_key="c" * 64,
                  protocol_epoch="synthetic-epoch", core_url="http://127.0.0.1:8765",
                  onebot_http_url="http://127.0.0.1:3000",
                  onebot_ws_url="ws://127.0.0.1:3001",
                  bridge_token=core.settings.bridge_token.get_secret_value())
    policy = OwnerGrantPolicy.model_validate({**policy.model_dump(),
        "purposes": ["chat", *policy.purposes], "capture": {
            "public_key": cfg.capture_public_key, "protocol_epoch": cfg.protocol_epoch,
            "identity_sha256": paid_budget.sha([cfg.owner_qq_id, cfg.bot_qq_id])}})
    result, _ = activate(core, policy, revision=1, operation="account-host-capture")
    return replace(cfg, owner_authority=result["authority"])
