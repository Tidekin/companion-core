from companion import paid_budget
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from tests.support.paid_model import approved_live


def prepare(core, tmp_path, *, enabled=True):
    approved_live(core, tmp_path)
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED" if enabled else "OFF")
    return OwnerGrantPolicy(owner_qq_id=core.settings.owner_qq_id, purposes=["evaluation"],
                            authorization_sha256="d" * 64,
                            paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)))
