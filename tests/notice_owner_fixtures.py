"""Explicit native notice selection and synthetic owner-purpose approval for tests."""

from companion import owner_authority, paid_budget
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from tests.support.owner_authority import activate
from tests.support.paid_model import approved_live


async def run_notice(core):
    # The selector owns activity choice; ordinary run_once now selects foreground work.
    job = core.store.one(
        "SELECT job_id FROM jobs WHERE kind='activity' ORDER BY "
        'record_sequence LIMIT 1'
    )
    assert job is not None
    return await core.run_once("notice-test", job_id=job["job_id"])


def activate_notice_authority(core, tmp_path, *, expires_at=None, **paid_limits):
    approved_live(core, tmp_path, **paid_limits)
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    policy = OwnerGrantPolicy(
        owner_qq_id=core.settings.owner_qq_id, purposes=["goals"],
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        expires_at=expires_at,
    )
    result, _ = activate(core, policy)
    assert owner_authority.current(core.store, core.settings, core.clock,
                                   purpose="goals").authority.as_dict() == result["authority"]
    return result["authority"]
