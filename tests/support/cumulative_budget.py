from companion import cumulative, paid_budget
from companion.store import uid
from tests.owner_model_fixtures import authorized_job
from tests.support.paid_model import approved_live


def policy(core, **changes):
    paid = core.settings.paid_model
    custody = paid_budget.journal(core.store, paid, allow_settlement=True)
    prior = custody["cumulative"]
    caps = dict(micro_usd=10_000_000, model_attempts=100, input_tokens=1_000_000,
                output_tokens=1_000_000, owner_inputs=100, send_attempts=100)
    inventory = changes.get("legacy_window_inventory")
    if inventory is not None:
        inventory = cumulative.LegacyWindowInventory.model_validate(inventory)
    values = dict(version=1, account_ref="b" * 64, policy_id=uid("quota"),
        approval_sha256=cumulative.digest(uid("approval")), account_evidence_sha256="d" * 64,
        credential_sha256=cumulative.credential(core.settings, "b" * 64),
        key_ownership_sha256="e" * 64, provider=paid.provider, currency=paid.currency,
        timezone=paid.timezone,
        owner_identity_sha256=cumulative.digest([core.settings.owner_qq_id,
                                                  core.settings.bot_qq_id]),
        starts_at=paid.starts_at, expires_at=paid.expires_at,
        expected_journal_head_sha256=custody["head_sha256"],
        predecessor_policy_sha256=prior["policy_sha256"],
        legacy_liabilities_sha256=cumulative.digest(cumulative.legacy_liabilities(
            core.store, paid.provider, inventory)) if not prior["policy"] else None,
        legacy_adoption_evidence_sha256="f" * 64,
        request={k: v for k, v in caps.items() if k not in {"owner_inputs", "send_attempts"}},
        window=caps, day=caps, month=caps, account=caps, concurrency=2)
    for key, value in changes.items():
        values[key] = ({**values[key], **value} if isinstance(value, dict)
                       and isinstance(values.get(key), dict) else value)
    scope = cumulative.CumulativeApproval.model_validate(values)
    core.settings.paid_model = paid.model_copy(update={"cumulative": scope})
    return scope


def enable(core, tmp_path, *, lease_seconds=None, **changes):
    approved_live(core, tmp_path)
    scope = policy(core, **changes)
    with core.store.transaction():
        cumulative.activate(core.store, core.settings, core.clock())
    authorized_job(core, lease_seconds=lease_seconds)
    return scope


def view(core):
    return paid_budget.journal(core.store, core.settings.paid_model,
                               allow_settlement=True)["cumulative"]
