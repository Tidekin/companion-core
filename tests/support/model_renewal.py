from datetime import timedelta

from companion import model_carry, owner_authority, paid_budget
from companion.model_carry_controller import verify
from companion.owner_authority_protocol import AuthorityStop
from companion_delivery_protocol import digest
from tests.model_carry_support import executors, observer


def renewal(core, case, *, number=1, offset=timedelta(seconds=1), open_ended=False,
            concurrency=None):
    active = case.successor["authority"]
    previous = paid_budget.snapshot(core.settings)
    expiry = core.settings.paid_model.expires_at
    core.clock.now = expiry + offset
    owner_authority.stop(
        core.store,
        core.settings,
        core.clock,
        AuthorityStop(operation_id=f"stop-renewal-{number}", authority=active),
    )
    expires = None if open_ended else expiry + timedelta(days=1)
    core.settings.paid_model = core.settings.paid_model.model_copy(
        update={
            "window_id": f"renewed-window-{number}",
            "approval_sha256": str(number) * 64,
            "expires_at": expires,
            "concurrency": concurrency or core.settings.paid_model.concurrency,
            "rates_valid_until": None if open_ended else max(
                core.settings.paid_model.rates_valid_until, expires),
        }
    )
    items = executors(core.clock())
    witness = verify(items, observe=observer(items))
    with core.store.transaction():
        view = owner_authority.custody(core.store, core.settings, settlement=True)
        value = model_carry.manifest(
            core.store, core.settings, active, core.settings.paid_model.window_id, items, view
        )
    policy = case.policy.model_copy(
        update={
            "authorization_sha256": str(number + 2) * 64,
            "unknown_carry_sha256": digest(value),
            "expires_at": expires,
            "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings)),
        }
    )
    request = {
        "version": 2,
        "operation_id": f"renewal-operation-{number}",
        "authorization_sha256": policy.authorization_sha256,
        "expected_head_sha256": view["head_sha256"],
        "authority": active,
        "successor_window_id": core.settings.paid_model.window_id,
        "manifest_sha256": digest(value),
        "successor_policy": policy.model_dump(mode="json"),
        "executors": items,
        "previous_paid_contract": previous,
    }
    return request, policy, witness
