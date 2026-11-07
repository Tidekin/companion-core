"""Automatic consent consumes the original draft, never another planner or queue."""

import pytest
from pydantic import ValidationError

from companion.life_protocol import LifeShadowApproval
from tests.support.owner_authority import activate
from tests.support.social_owner_authority import stop
from tests.test_life_expression_consumption import (
    close_provider_clients as close_provider_clients,
)
from tests.test_life_expression_consumption import prepared

CHANNELS = [
    ("qq.private.proactive", "PRIVATE_MESSAGE_DRAFT", ["proactive"]),
    ("diary.save", "PRIVATE_DIARY", ["diary"]),
    ("qzone.publish", "QZONE_DRAFT", ["social", "account_actions"]),
]


@pytest.mark.parametrize("action,destination,purposes", CHANNELS)
@pytest.mark.parametrize("automatic", [False, True])
def test_explicit_policy_opt_in_is_required_and_consumes_original_once(
    core, tmp_path, action, destination, purposes, automatic,
):
    core.settings.diary_enabled = True
    prepared(core, tmp_path, destination=destination, purposes=purposes,
             automatic_actions=[action] if automatic else [])
    usage, calls = core.store.rows("SELECT * FROM model_usage"), list(core.model.calls)
    assert core.life_topics.consume_due(diary=core.diary) is automatic
    assert not core.life_topics.consume_due(diary=core.diary)
    assert len(core.store.rows("SELECT * FROM outbox")) == int(automatic and action != "diary.save")
    assert len(core.store.rows("SELECT * FROM diary_entries")) == int(
        automatic and action == "diary.save")
    assert core.store.rows("SELECT * FROM model_usage") == usage and core.model.calls == calls


@pytest.mark.parametrize("action,destination,purposes", CHANNELS)
def test_automatic_choice_never_substitutes_for_independent_channel_purpose(
    core, tmp_path, action, destination, purposes,
):
    core.settings.diary_enabled = True
    prepared(core, tmp_path, destination=destination, purposes=[], automatic_actions=[action])
    before = core.store.db.total_changes
    assert not core.life_topics.consume_due(diary=core.diary)
    assert core.store.db.total_changes == before
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.store.rows("SELECT * FROM diary_entries") == []


@pytest.mark.parametrize("fence", ["off", "quiet", "pause", "stop", "erase", "quota"])
def test_automatic_private_expression_preserves_original_fences(core, tmp_path, fence):
    _, _, authority, source = prepared(core, tmp_path, destination="PRIVATE_MESSAGE_DRAFT",
        automatic_actions=["qq.private.proactive"])
    if fence == "off":
        core.settings.life_loop.mode = "OFF"
    elif fence == "quiet":
        core.settings.quiet_start, core.settings.quiet_end = 0, 23
    elif fence == "pause":
        core.goals.pause_autonomy(True)
    elif fence == "stop":
        stop(core, authority)
    elif fence == "erase":
        with core.store.transaction():
            core.ledger.expire_events(core.store, [source], core.clock())
    else:
        core.settings.proactive_daily_limit = 0
    usage = core.store.rows("SELECT * FROM model_usage")
    assert not core.life_topics.consume_due(diary=core.diary)
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.store.rows("SELECT * FROM model_usage") == usage


def test_automatic_qzone_never_promotes_exact_granted_private_draft(core, tmp_path):
    job, _, _, _ = prepared(core, tmp_path, private=True, automatic_actions=["qzone.publish"])
    assert core.life_topics.retained_result(job)["qzone_draft"]
    usage = core.store.rows("SELECT * FROM model_usage")
    assert not core.life_topics.consume_due(diary=core.diary)
    assert core.store.rows("SELECT * FROM social_autonomy_plans") == []
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.store.rows("SELECT * FROM model_usage") == usage


def test_automatic_policy_is_default_empty_and_rejects_unbounded_actions(core, tmp_path):
    _, policy, _, _ = prepared(core, tmp_path)
    scope = policy.life_shadow
    assert scope.automatic_actions == []
    legacy = scope.model_dump(mode="json", exclude={"automatic_actions"})
    assert "automatic_actions" not in scope.model_dump(mode="json")
    assert LifeShadowApproval.model_validate(legacy).model_dump(mode="json") == legacy
    for actions in (["qq.private.proactive"] * 2, ["qzone.comment"], ["rem.publish"]):
        with pytest.raises(ValidationError):
            LifeShadowApproval.model_validate({**scope.model_dump(), "automatic_actions": actions})


def test_new_automatic_consent_cannot_rebind_a_prior_manual_draft(core, tmp_path):
    _, policy, _, _ = prepared(core, tmp_path, destination="PRIVATE_MESSAGE_DRAFT")
    scope = policy.life_shadow.model_copy(update={"automatic_actions": ["qq.private.proactive"]})
    activate(core, policy.model_copy(update={"life_shadow": scope}), revision=1,
             operation="explicit-automatic-consent")
    before = core.store.db.total_changes
    assert not core.life_topics.consume_due(diary=core.diary)
    assert core.store.db.total_changes == before
    assert core.store.rows("SELECT * FROM outbox") == []
