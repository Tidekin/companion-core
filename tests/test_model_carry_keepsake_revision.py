"""Under a carried UNKNOWN, only the keepsake grant itself may be added or revoked."""

import pytest

from companion.model_carry import purpose_revision
from companion.owner_authority_protocol import KeepsakePolicy, OwnerGrantPolicy

SHA = "a" * 64


def policy(**changes):
    return OwnerGrantPolicy(**{
        "owner_qq_id": "123456", "purposes": ["memory", "diary"], "memory_scope": "owner_private",
        "authorization_sha256": SHA, "paid_contract_sha256": SHA, **changes})


def test_adding_or_revoking_only_the_keepsake_grant_is_admitted():
    plain, kept = policy(), policy(keepsake=KeepsakePolicy())
    purpose_revision(plain, kept)
    purpose_revision(kept, plain)
    purpose_revision(kept, kept)


@pytest.mark.parametrize("changes", [
    {"source_retention_days": 30},
    {"authorization_sha256": "b" * 64},
    {"purposes": ["memory", "diary", "evaluation"], "authorization_sha256": "b" * 64},
])
def test_keepsake_cannot_carry_any_other_change(changes):
    with pytest.raises(ValueError):
        purpose_revision(policy(), policy(keepsake=KeepsakePolicy(), **changes))


def test_changing_keepsake_bounds_is_not_a_grant_or_revocation():
    with pytest.raises(ValueError):
        purpose_revision(policy(keepsake=KeepsakePolicy()),
                         policy(keepsake=KeepsakePolicy(max_items=20000)))
