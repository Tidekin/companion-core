"""Under a carried UNKNOWN, the profile purpose may be appended on its own."""

import pytest

from companion.model_carry import purpose_revision
from companion.owner_authority_protocol import OwnerGrantPolicy

SHA = "a" * 64


def policy(**changes):
    return OwnerGrantPolicy(**{
        "owner_qq_id": "123456", "purposes": ["chat", "memory"], "memory_scope": "owner_private",
        "authorization_sha256": SHA, "paid_contract_sha256": SHA, **changes})


def test_appending_only_profile_with_a_new_approval_is_admitted():
    purpose_revision(policy(), policy(purposes=["chat", "memory", "profile"],
                                      authorization_sha256="b" * 64))


@pytest.mark.parametrize("changes", [
    {"purposes": ["chat", "memory", "profile"]},
    {"purposes": ["profile", "chat", "memory"], "authorization_sha256": "b" * 64},
    {"purposes": ["chat", "memory", "profile"], "authorization_sha256": "b" * 64,
     "source_retention_days": 30},
    {"purposes": ["chat", "memory", "content"], "authorization_sha256": "b" * 64},
])
def test_profile_cannot_carry_any_other_change(changes):
    with pytest.raises(ValueError):
        purpose_revision(policy(), policy(**changes))


def test_a_grant_may_hold_every_purpose_once():
    from typing import get_args

    from companion.owner_authority_protocol import Purpose

    every = list(get_args(Purpose))
    assert len(every) > 25
    allowed = OwnerGrantPolicy.model_fields["purposes"]
    assert any(getattr(item, "max_length", None) == len(every) for item in allowed.metadata)
    with pytest.raises(ValueError):
        policy(purposes=["chat", "chat"])
