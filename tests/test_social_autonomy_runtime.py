"""Autonomous selection crosses the real planner, native budget and original Outbox."""

import json

import pytest

from companion.reading_protocol import ReadObservation
from companion.social_autonomy_protocol import ProfileBaseline
from companion.social_autonomy_runtime import run_once
from companion.store import after
from tests.conftest import ingest, run
from tests.social_profile_support import confirmed_profile_material
from tests.support.reading_protocol import envelope
from tests.support.social_autonomy import (
    MockPlanner,
    avatar,
    background,
    extra_public,
    planned,
    read_public,
    service,
)


def tick(module):
    return run(run_once(module.store, module.settings, module.model, module.clock))


def probe(module, field="nickname", value="观星者"):
    module.record_profile(ProfileBaseline(bot_qq_id=module.settings.bot_qq_id, field=field,
                                          value=value, observed_at=module.clock(),
                                          receipt_sha256="c" * 64))


def test_disabled_runtime_has_no_plan_fee_or_outbox(core):
    model = MockPlanner()
    assert run(run_once(core.store, core.settings, model, core.clock)) is False
    assert not model.calls
    for table in ("social_autonomy_plans", "model_usage", "outbox"):
        assert not core.store.rows("SELECT * FROM " + table)


def test_public_evidence_without_direct_owner_binding_cannot_start_decision(core):
    read_public(core, "bilibili")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    module = service(core)
    module.model = MockPlanner()
    assert tick(module) is False
    assert not module.model.calls
    assert not core.store.rows("SELECT * FROM social_autonomy_plans")


@pytest.mark.parametrize("changes", [
    {"qzone_enabled": False},
    {"qzone_enabled": False, "nickname_enabled": True},
    {"qzone_enabled": False, "avatar_enabled": True, "asset_policy_sha256": "e" * 64},
])
def test_no_eligible_action_does_not_accumulate_skipped_rows(core, changes):
    module, _, _ = background(core, **changes)
    for _ in range(3):
        assert tick(module) is False
    assert not module.model.calls
    assert not core.store.rows("SELECT * FROM social_autonomy_plans")


def test_public_selection_plans_once_and_inserts_original_account_outbox(core):
    module, ref, conversation = background(core)
    assert tick(module) is True
    native = core.store.one("SELECT * FROM outbox")
    assert (native["state"], native["delivery_lane"], native["conversation_id"]) == (
        "PENDING", "qq_account", conversation,
    )
    assert native["origin_event_id"] == ref
    assert native["attempt_count"] == 0
    assert native["provider_message_id"] is None
    frozen = json.loads(native["content_json"])
    assert frozen["audience"] == {"ugc_right": 16, "target_uins": ["10001"]}
    usage = core.store.one("SELECT * FROM model_usage")
    assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] > 0
    for _ in range(3):
        assert tick(module) is False
    assert len(module.model.calls) == 1
    assert core.store.rows("SELECT * FROM model_usage") == [usage]
    assert core.store.rows("SELECT * FROM outbox") == [native]


def test_skip_is_not_repaid_when_same_public_content_is_fetched_again(core):
    module, ref, _ = background(core)
    module.model.proposal = {"kind": "skip", "reason_code": "NO_SUITABLE_SOURCE"}
    assert tick(module) is True
    core.clock.advance(1)
    second = extra_public(core)
    assert second != ref
    assert tick(module) is False
    assert len(module.model.calls) == 1
    assert core.store.one("SELECT state FROM social_autonomy_plans")["state"] == "SKIPPED"
    assert not core.store.rows("SELECT * FROM outbox")


def test_new_receipt_for_same_profile_value_does_not_restart_a_skip(core):
    confirmed_profile_material(core)
    module, _, _ = background(core, qzone_enabled=False, nickname_enabled=True)
    probe(module)
    module.model.proposal = {"kind": "skip", "reason_code": "KEEP_CURRENT_PROFILE"}
    assert tick(module) is True
    core.clock.advance(1)
    probe(module)
    assert tick(module) is False
    assert len(module.model.calls) == 1


def test_missing_and_stale_profile_fields_are_excluded_from_model_choices(core):
    confirmed_profile_material(core)
    module, _, _ = background(core, qzone_enabled=False, nickname_enabled=True,
                               signature_enabled=True, avatar_enabled=True,
                               asset_policy_sha256="e" * 64, profile_baseline_seconds=3)
    probe(module, "signature", "过期的真实资料")
    core.clock.advance(4)
    probe(module)
    module.model.proposal = {"kind": "nickname", "value": "星河"}
    assert tick(module) is True
    payload = module.model.calls[0]
    assert payload["allowed_proposals"] == ["skip", "nickname"]
    assert "current_profile" not in payload and "avatar_candidates" not in payload
    assert payload["owner_confirmed_material"]
    assert core.store.one("SELECT action_type FROM outbox")["action_type"] == "qq.profile.nickname"


def test_avatar_choices_include_only_current_reviewed_assets_and_real_baseline(core):
    module, _, _ = background(core, qzone_enabled=False, avatar_enabled=True,
                               asset_policy_sha256="e" * 64)
    probe(module, "avatar", "f" * 64)
    valid = avatar(core)
    module.register_asset(valid)
    module.register_asset(avatar(core, asset_id="expired", expires_at=after(core.clock(), 1)))
    module.register_asset(avatar(core, asset_id="revoked"))
    core.store.execute("UPDATE social_autonomy_assets SET revoked_at=%s WHERE asset_id='revoked'",
                       (core.clock(),))
    core.clock.advance(2)
    module.model.proposal = {"kind": "avatar", "asset_id": valid.asset_id}
    assert tick(module) is True
    payload = module.model.calls[0]
    assert payload["avatar_candidates"] == [{"asset_id": valid.asset_id,
                                               "description": valid.description}]
    assert payload["allowed_proposals"] == ["skip", "avatar"]
    assert "https://art.example.test" not in json.dumps(payload)


def test_existing_ready_plan_is_enqueued_without_a_second_model_call(core):
    module, ref, _ = background(core)
    result = planned(module, ref)
    assert tick(module) is True
    assert len(module.model.calls) == 1
    reservation = core.store.one("SELECT * FROM social_autonomy_reservations")
    assert reservation["plan_id"] == result["plan_id"]


def test_expired_unreserved_plan_becomes_terminal_without_refunding_or_retry(core):
    module, ref, _ = background(core, plan_ttl_seconds=1)
    planned(module, ref)
    usage = core.store.rows("SELECT * FROM model_usage")
    core.clock.advance(2)
    assert tick(module) is True
    assert core.store.one("SELECT state,last_error_code FROM social_autonomy_plans") == {
        "state": "CANCELLED", "last_error_code": "SOCIAL_PLAN_EXPIRED",
    }
    assert tick(module) is False
    assert core.store.rows("SELECT * FROM model_usage") == usage
    assert not core.store.rows("SELECT * FROM outbox")


def test_invalid_public_source_cancels_ready_plan_without_an_outbox(core):
    module, ref, _ = background(core)
    planned(module, ref)
    core.store.execute('UPDATE events SET redacted=1 WHERE event_id=%s', (ref,))
    assert tick(module) is True
    assert core.store.one("SELECT state FROM social_autonomy_plans")["state"] == "CANCELLED"
    assert tick(module) is False
    assert len(module.model.calls) == 1
    assert not core.store.rows("SELECT * FROM outbox")


def test_pending_chat_yields_before_model_and_before_reserving_existing_plan(core):
    module, ref, _ = background(core)
    ingest(core, 100, "先回答我", origin="onebot-direct:FriendMessage:10001")
    assert tick(module) is False
    assert not module.model.calls
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    planned(module, ref)
    ingest(core, 101, "继续聊天", origin="onebot-direct:FriendMessage:10001")
    assert tick(module) is False
    assert not core.store.rows("SELECT * FROM outbox")
    assert len(module.model.calls) == 1


def test_chat_arriving_during_model_keeps_ready_plan_until_foreground_finishes(core):
    module, _, _ = background(core)
    module.model.callback = lambda: ingest(core, 100, "先聊这件事",
                                            origin="onebot-direct:FriendMessage:10001")
    assert tick(module) is True
    assert core.store.one("SELECT state FROM social_autonomy_plans")["state"] == "READY"
    assert not core.store.rows("SELECT * FROM outbox")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    assert tick(module) is True
    assert len(module.model.calls) == 1
    assert core.store.one("SELECT state FROM outbox")["state"] == "PENDING"


def test_unknown_model_is_durable_and_new_evidence_cannot_trigger_repayment(core):
    module, _, _ = background(core)
    module.model.error = RuntimeError("synthetic response loss")
    assert tick(module) is True
    usage = core.store.one("SELECT * FROM model_usage")
    assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] > 0
    core.clock.advance(1)
    extra_public(core)
    confirmed_profile_material(core)
    next_module = service(core, nickname_enabled=True, decision_interval_seconds=0)
    next_module.model = module.model
    probe(next_module)
    next_module.model.error = None
    assert tick(next_module) is False
    assert len(module.model.calls) == 1
    assert core.store.rows("SELECT * FROM model_usage") == [usage]


@pytest.mark.parametrize("changes", [
    {"model_daily_calls": 0}, {"model_daily_calls": 1}, {"decision_interval_seconds": 3600},
])
def test_model_limits_are_idle_fences_and_do_not_crash_the_worker(core, changes):
    module, _, _ = background(core, **changes)
    module.model.proposal = {"kind": "skip", "reason_code": "NO_ACTION"}
    expected = changes.get("model_daily_calls") != 0
    assert tick(module) is expected
    count = len(module.model.calls)
    probe_enabled = service(core, **{"nickname_enabled": True, **changes})
    probe_enabled.model = module.model
    probe(probe_enabled)
    assert tick(probe_enabled) is False
    assert len(module.model.calls) == count


def test_blocked_ready_plan_does_not_accumulate_new_decisions(core):
    module, _, _ = background(core, qzone_daily_limit=0)
    assert tick(module) is True
    assert core.store.one("SELECT state FROM social_autonomy_plans")["state"] == "READY"
    for _ in range(3):
        assert tick(module) is False
    assert len(module.model.calls) == 1
    assert not core.store.rows("SELECT * FROM outbox")


def test_runtime_selects_a_smaller_pack_that_fits_the_native_input_bound(core):
    module, ref, _ = background(core)
    body = envelope("heybox")
    with core.store.transaction():
        request = core.exploration.create("heybox", "read", "social-runtime-heybox",
                                          body["observations"][0]["url"])
    item = core.reading.lease("heybox")
    body["lease_token"] = item["lease_token"]
    body["observations"][0]["provenance"] = item["context"]
    body["observations"][0]["fetched_at"] = core.clock()
    body["observations"][0]["evidence"]["fetched_at"] = core.clock()
    core.reading.complete("heybox", request, ReadObservation.model_validate(body))
    messages, _ = module._messages(module.context(source_ids=[ref]), module.config)
    single_size = sum(len(message["content"].encode()) for message in messages)
    config = module.config.model_copy(update={"max_input_bytes": single_size + 50})
    core.settings.social_autonomy = config
    assert tick(module) is True
    assert len(module.model.calls[0]["sources"]) == 1
    assert core.store.one("SELECT state FROM outbox")["state"] == "PENDING"


def test_impossibly_small_prompt_budget_remains_idle_without_charging(core):
    module, _, _ = background(core, max_input_bytes=1024)
    for _ in range(2):
        assert tick(module) is False
    assert not module.model.calls
    assert not core.store.rows("SELECT * FROM social_autonomy_plans")
