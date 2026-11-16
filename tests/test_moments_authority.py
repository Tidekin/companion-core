"""The moments permission is its own purpose; under a carry it may only be added or removed."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from companion.life_protocol import LifeShadowApproval
from companion.model_carry import purpose_revision
from companion.owner_authority_protocol import OwnerGrantPolicy

SHA = "a" * 64
LIFE = LifeShadowApproval(
    scope="owner_private_life_shadow_v1", authorization_sha256="c" * 64,
    conversation_id="conv_native_owner", source_event_ids=["evt_source"],
    purposes=["PRIVATE_DIARY", "PRIVATE_MESSAGE_DRAFT"],
    expires_at=datetime.now(UTC) + timedelta(days=3), max_items=5,
    automatic_actions=["diary.save"])


def policy(purposes=("life", "diary", "proactive"), life=LIFE, **changes):
    return OwnerGrantPolicy(**{"owner_qq_id": "123456", "purposes": list(purposes),
                               "authorization_sha256": SHA, "paid_contract_sha256": SHA,
                               "life_shadow": life, **changes})


def granted(life=LIFE, purposes=("life", "diary", "proactive")):
    return policy([*purposes, "moments"], life.model_copy(update={
        "purposes": [*life.purposes, "PRIVATE_MOMENT"],
        "automatic_actions": [*life.automatic_actions, "moment.save"]}))


def test_moments_needs_life():
    with pytest.raises(ValidationError, match="Life"):
        OwnerGrantPolicy(owner_qq_id="123456", purposes=["moments"], authorization_sha256=SHA,
                         paid_contract_sha256=SHA)


def test_carry_admits_exactly_the_moments_grant_and_its_revocation():
    purpose_revision(policy(), granted())
    purpose_revision(granted(), policy())


def changed(variant):
    after = granted()
    if variant == "no_destination":
        return policy([*after.purposes], after.life_shadow.model_copy(
            update={"purposes": [*LIFE.purposes, "PRIVATE_CREATION"]}))
    if variant == "no_action":
        return policy([*after.purposes], after.life_shadow.model_copy(
            update={"automatic_actions": LIFE.automatic_actions}))
    if variant == "extra_change":
        return after.model_copy(update={"source_retention_days": 30})
    return policy(["moments", "life", "diary", "proactive"], after.life_shadow)


@pytest.mark.parametrize("variant", ["no_destination", "no_action", "extra_change", "order"])
def test_carry_rejects_partial_or_combined_moment_changes(variant):
    with pytest.raises(ValueError):
        purpose_revision(policy(), changed(variant))
