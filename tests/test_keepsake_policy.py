"""The keepsake grant is explicit, bounded and invisible to policies that omit it."""

import pytest
from pydantic import ValidationError

from companion.owner_authority_protocol import KeepsakePolicy, OwnerGrantPolicy

SHA = "a" * 64


def policy(**changes):
    values = {"owner_qq_id": "123456", "purposes": ["memory"], "authorization_sha256": SHA,
              "paid_contract_sha256": SHA, "memory_scope": "owner_private", **changes}
    return OwnerGrantPolicy(**values)


def test_absent_keepsake_leaves_existing_policy_digest_input_unchanged():
    dumped = policy().model_dump(mode="json")
    assert "keepsake" not in dumped
    assert OwnerGrantPolicy.model_validate(dumped).keepsake is None


def test_default_grant_keeps_until_revoked_with_large_bounded_capacity():
    grant = policy(keepsake=KeepsakePolicy()).keepsake
    assert grant.retention_days is None
    assert (grant.max_items, grant.max_excerpt_chars) == (10000, 120)
    assert policy(keepsake={}).model_dump(mode="json")["keepsake"] == {
        "retention_days": None, "max_items": 10000, "max_excerpt_chars": 120}


@pytest.mark.parametrize("changes", [
    {"keepsake": {"retention_days": 365}},
    {"keepsake": {"max_excerpt_chars": 301}},
    {"keepsake": {"max_items": 0}},
    {"keepsake": {"unknown": 1}},
])
def test_keepsake_requires_memory_scope_and_bounded_values(changes):
    values = {"keepsake": {}, **changes}
    with pytest.raises(ValidationError):
        policy(**values)


def test_finite_keepsake_must_outlast_source_retention():
    assert policy(keepsake={"retention_days": 366}).keepsake.retention_days == 366


def test_keepsake_without_owner_private_memory_is_rejected():
    assert policy(purposes=["diary"], memory_scope="none").keepsake is None
    with pytest.raises(ValidationError, match="owner-private memory"):
        policy(purposes=["diary"], memory_scope="none", keepsake={})
