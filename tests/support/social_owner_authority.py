import pytest

from companion import owner_authority, paid_budget
from companion.owner_authority_protocol import AuthorityStop, OwnerGrantPolicy, OwnerRuntimeConfig
from tests.conftest import run
from tests.social_model_transport import NativeTestModel
from tests.support.owner_authority import activate
from tests.support.paid_model import approved_live


@pytest.fixture(autouse=True)
def close_provider_clients(core):
    core.authority_test_clients = []
    yield
    for client in core.authority_test_clients:
        run(client.aclose())


def authorize(core, tmp_path, *, life=None, account=True, module=None, additional_purposes=()):
    approved_live(core, tmp_path, max_output_tokens=4096, )
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    purposes = (["life"] if life else ["social"]) + list(additional_purposes)
    capability = {}
    if account and not life:
        purposes.append("account_actions")
    if "account_actions" in purposes:
        capability["account_actions"] = core.settings.social_autonomy.authorization_sha256
    if "qzone_publication" in purposes:
        capability["qzone_publication"] = owner_authority.capability_hashes(core.settings)[
            "qzone_publication"]
    policy = OwnerGrantPolicy(
        owner_qq_id=core.settings.owner_qq_id, purposes=purposes,
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capability_policies=capability, life_shadow=life,
        memory_scope="owner_private" if "memory" in purposes else "none",
    )
    result, _ = activate(core, policy)
    target = module if module is not None else core
    if (life is not None or module is not None) and not isinstance(target.model, NativeTestModel):
        target.model = NativeTestModel(core.settings, target.model,
                                       "social_autonomy" if module else "activity")
        core.authority_test_clients.append(target.model.client)
    return policy, result["authority"]


def stop(core, ref):
    return owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="stop-native-social", authority=ref,
    ))
