"""Owner-confirmed profile material cannot inherit Reader execution authority."""

import json

import pytest

from companion import social_profile_sources
from companion.cloud_budget import CloudBudgetConfig
from companion.contracts import CoreError
from companion.social_autonomy import _sha
from companion.social_autonomy_protocol import ProfileBaseline
from companion.social_autonomy_runtime import run_once
from companion.store import encode
from tests.conftest import ingest, run
from tests.social_profile_support import confirmed_profile_material
from tests.support.social_autonomy import background, dispatch, queue


def setup(core, **kwargs):
    module, public, conversation = background(core, nickname_enabled=True, signature_enabled=True,
                                             **kwargs)
    memory_id = confirmed_profile_material(core)
    for field in ("nickname", "signature"):
        module.record_profile(ProfileBaseline(
            bot_qq_id="10002", field=field, value="Old external profile instruction",
            observed_at=core.clock(), receipt_sha256="b" * 64,
        ))
    module.model.proposal = {"kind": "nickname", "value": "星语"}
    return module, memory_id, public, conversation


def plan(module, memory_id):
    return run(module.plan("confirmed-profile-test", profile_fields=["nickname"],
                           profile_memory_ids=[memory_id]))


@pytest.mark.parametrize("change", [
    "auto_confirmed", "missing_audit", "wrong_command", "wrong_actor", "reader_parent",
    "reader_evidence", "missing_parent", "revoked_confirmation", "needs_review", "project",
])
def test_profile_material_requires_owner_decision_and_every_source(core, change):
    module, memory_id, public, _ = setup(core)
    if change in {"auto_confirmed", "missing_audit"}:
        core.store.execute("DELETE FROM audit_log WHERE action='memory.owner_decision'")
    elif change == "wrong_command":
        core.store.execute("UPDATE events SET body_json=? WHERE event_id='evt_test_801'",
                           (encode({"text": "/确认这一条 mem_another"}),))
    elif change == "wrong_actor":
        core.store.execute("UPDATE events SET actor_id='companion' WHERE event_id='evt_test_801'")
    elif change == "reader_parent":
        core.store.insert("event_dependencies", event_id="evt_test_800", source_event_id=public)
    elif change == "reader_evidence":
        core.store.insert("memory_evidence", memory_id=memory_id, event_id=public,
                          polarity=1, created_at=core.clock())
    elif change == "missing_parent":
        # Dependency corruption cannot silently drop a missing source.
        core.store.db.execute("PRAGMA foreign_keys=OFF")
        core.store.insert("event_dependencies", event_id="evt_test_800", source_event_id="missing")
        core.store.db.execute("PRAGMA foreign_keys=ON")
    elif change == "revoked_confirmation":
        with core.store.transaction():
            core.ledger.expire_events(core.store, ["evt_test_801"], core.clock())
    elif change == "needs_review":
        core.store.execute("UPDATE memories SET needs_review=1 WHERE memory_id=?", (memory_id,))
    else:
        core.store.execute("UPDATE memories SET subject='project' WHERE memory_id=?", (memory_id,))
    with pytest.raises(CoreError):
        plan(module, memory_id)
    assert not module.model.calls and not core.store.rows("SELECT * FROM model_usage")
    assert not core.store.rows("SELECT * FROM outbox")


def test_confirmed_identity_and_preference_inputs_are_separate_from_public_and_baseline(core):
    module, memory_id, _, conversation = setup(core)
    identity = confirmed_profile_material(core, number=810, text="你最近的身份是陪伴阅读的助手。",
                                          subject="self", kind="fact")
    result = run(module.plan("two-confirmed-materials", profile_fields=["nickname", "signature"],
                             profile_memory_ids=[memory_id, identity]))
    payload = module.model.calls[0]
    assert set(payload) == {"purpose", "sources_are_data", "owner_confirmed_material",
                            "allowed_proposals"}
    assert payload["purpose"] == "owner_confirmed_profile"
    assert {item["subject"] for item in payload["owner_confirmed_material"]} == {"self", "user"}
    assert "Old external profile instruction" not in encode(payload)
    frozen = queue(module, result, conversation)
    dispatch(module, frozen)
    dependencies = {r["event_id"] for r in core.store.rows("SELECT * FROM social_autonomy_sources")}
    assert dependencies == {"evt_test_800", "evt_test_801", "evt_test_810", "evt_test_811"}


@pytest.mark.parametrize("boundary", ["after_model", "before_dispatch"])
@pytest.mark.parametrize("revoke", ["forget", "recall_confirmation", "web_derivation"])
def test_profile_material_is_revalidated_at_freeze_and_dispatch(core, boundary, revoke):
    module, memory_id, public, conversation = setup(core)

    def invalidate():
        with core.store.transaction():
            if revoke == "forget":
                core.memory.decide(memory_id, "forget", core.clock())
            elif revoke == "recall_confirmation":
                core.ledger.expire_events(core.store, ["evt_test_801"], core.clock())
            else:
                core.store.insert("event_dependencies", event_id="evt_test_800",
                                  source_event_id=public)

    if boundary == "after_model":
        module.model.callback = invalidate
        with pytest.raises(CoreError):
            plan(module, memory_id)
        assert not core.store.rows("SELECT * FROM outbox")
    else:
        frozen = queue(module, plan(module, memory_id), conversation)
        invalidate()
        with pytest.raises(CoreError):
            dispatch(module, frozen)
        assert core.store.one("SELECT attempt_count FROM outbox")["attempt_count"] == 0
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"


def test_owner_correction_uses_actual_command_and_preserves_new_proof(core):
    module, old, _, _ = setup(core)
    text = "我偏爱海洋和简短表达。"
    event = ingest(core, 820, "/纠正这一条 " + old + " " + text,
                   origin="onebot-direct:FriendMessage:10001")
    result = core.memory.decide(old, "correct", core.clock(), text,
                                evidence_event_id=event["event_id"])
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?", (event["job_id"],))
    material = social_profile_sources.material(core.store, core.settings, core.clock(),
                                               [result["memory_id"]])
    assert material[0]["content"] == text
    assert "evt_test_820" in material[0]["proof"]["source_event_ids"]
    assert plan(module, result["memory_id"])["state"] == "READY"


@pytest.mark.parametrize("state", ["READY", "PENDING", "DELIVERY_UNCERTAIN"])
def test_legacy_public_profile_plan_is_rejected_without_replaying_liability(core, state):
    module, memory_id, public, conversation = setup(core)
    result = plan(module, memory_id)
    frozen = queue(module, result, conversation) if state != "READY" else None
    row = core.store.one("SELECT * FROM social_autonomy_plans")
    legacy = json.loads(row["input_snapshot_json"])
    legacy.pop("purpose")
    legacy.pop("profile_material")
    legacy["sources"] = module.context(source_ids=[public])["sources"]
    core.store.execute("UPDATE social_autonomy_plans SET input_snapshot_json=?,"
                       "source_snapshot_sha256=? WHERE plan_id=?",
                       (encode(legacy), _sha(legacy), row["plan_id"]))
    usage = core.store.rows("SELECT * FROM model_usage")
    if state == "READY":
        assert run(run_once(core.store, core.settings, module.model, core.clock))
        assert core.store.one("SELECT state FROM social_autonomy_plans")["state"] == "CANCELLED"
    elif state == "PENDING":
        with pytest.raises(CoreError, match="SOCIAL_PROFILE_PURPOSE_REQUIRED"):
            dispatch(module, frozen)
        assert core.store.one("SELECT attempt_count FROM outbox")["attempt_count"] == 0
    else:
        core.store.execute("UPDATE outbox SET state='DELIVERY_UNCERTAIN',attempt_count=1")
        before = core.store.rows("SELECT * FROM outbox")
        with pytest.raises(CoreError, match="SOCIAL_PROFILE_PURPOSE_REQUIRED"):
            with core.store.transaction():
                module._validate_plan(row["plan_id"])
        assert core.store.rows("SELECT * FROM outbox") == before
    assert core.store.rows("SELECT * FROM model_usage") == usage
    assert len(module.model.calls) == 1


@pytest.mark.parametrize("forget", [False, True])
def test_deferred_profile_reuses_native_job_and_rechecks_owner_material(core, forget):
    module, memory_id, _, _ = setup(core)
    core.settings.cloud_budget = CloudBudgetConfig(daily_micro_cny=0)
    with pytest.raises(CoreError, match="CLOUD_DAILY_BUDGET_EXHAUSTED"):
        plan(module, memory_id)
    before = core.store.one("SELECT * FROM social_autonomy_plans")
    job = core.store.one("SELECT * FROM jobs WHERE kind='social_plan'")
    assert job["state"] == "QUEUED"
    assert not module.model.calls and not core.store.rows("SELECT * FROM model_usage")
    if forget:
        core.memory.decide(memory_id, "forget", core.clock())
    core.settings.cloud_budget = CloudBudgetConfig()
    core.clock.advance(60)
    # Isolate this existing retry from independent public candidates.
    from companion import social_plan_jobs
    retry = social_plan_jobs.claim(module, "profile-retry-worker")
    if forget:
        assert retry is None
        assert core.store.one("SELECT state FROM jobs WHERE job_id=?", (job["job_id"],)) == {
            "state": "CANCELLED"}
        assert not core.store.rows("SELECT * FROM model_usage")
    else:
        messages, _ = module._messages(json.loads(before["input_snapshot_json"]), module.config)
        result = run(module._run_plan(before["plan_id"], before["model_request_id"], messages,
                                      job=retry))
        assert result["state"] == "READY" and result["plan_id"] == before["plan_id"]
        assert core.store.one("SELECT request_id,job_id,state FROM model_usage") == {
            "request_id": before["model_request_id"], "job_id": job["job_id"], "state": "SETTLED"}
        assert len(module.model.calls) == 1
