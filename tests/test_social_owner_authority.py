"""Native Life/Social custody under owner revisions; external model only is fake."""

import json

import pytest
from pydantic import ValidationError

from companion import account_actions, notices, owner_authority, paid_budget, social_plan_jobs
from companion.affect import AffectEngine
from companion.cloud_budget import CloudBudgetConfig
from companion.contracts import CoreError
from companion.life_protocol import LifeShadowApproval
from companion.owner_authority_protocol import AuthorityStop, OwnerGrantPolicy, OwnerRuntimeConfig
from companion.store import after, uid
from tests.conftest import ingest, run
from tests.social_model_transport import NativeTestModel
from tests.test_life_topics import bug, due, proposal
from tests.test_owner_authority import activate
from tests.test_paid_model_gate import approved_live
from tests.test_social_autonomy import background, planned, queue


@pytest.fixture(autouse=True)
def close_provider_clients(core):
    core.authority_test_clients = []
    yield
    for client in core.authority_test_clients:
        run(client.aclose())


def authorize(core, tmp_path, *, life=None, account=True, module=None, additional_purposes=()):
    approved_live(core, tmp_path, max_output_tokens=4096, per_attempt_micro_usd=100000,
                  per_request_micro_usd=200000)
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    purposes = (["life"] if life else ["social"]) + list(additional_purposes)
    capability = {}
    if account and not life:
        purposes.append("account_actions")
    if "account_actions" in purposes:
        capability["account_actions"] = core.settings.social_autonomy.authorization_sha256
    if "qzone_publication" in purposes:
        capability["qzone_publication"] = owner_authority.capability_hashes(core.settings)[
            "qzone_publication"]
    policy = OwnerGrantPolicy(
        owner_qq_id=core.settings.owner_qq_id, purposes=purposes,
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capability_policies=capability, life_shadow=life,
        memory_scope="owner_private" if "memory" in purposes else "none",
    )
    result, _ = activate(core, policy)
    target = module if module is not None else core
    if (life is not None or module is not None) and not isinstance(target.model, NativeTestModel):
        target.model = NativeTestModel(core.settings, target.model,
                                       "social_autonomy" if module else "activity")
        core.authority_test_clients.append(target.model.client)
    return policy, result["authority"]


def life_authorized(core, tmp_path):
    topic = bug(core)
    scope = LifeShadowApproval(
        scope="owner_private_life_shadow_v1", authorization_sha256="c" * 64,
        conversation_id=topic["conversation_id"], source_event_ids=["evt_test_1"],
        purposes=["PRIVATE_MESSAGE_DRAFT", "PRIVATE_DIARY"], max_items=4,
        expires_at=after(core.clock(), 3 * 86400),
    )
    return authorize(core, tmp_path, life=scope)


def stop(core, ref):
    return owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="stop-native-social", authority=ref,
    ))


def test_life_job_pins_original_owner_revision_and_never_rebinds(core, tmp_path):
    policy, ref = life_authorized(core, tmp_path)
    job_id = due(core)
    assert job_id
    saved = core.store.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
    assert json.loads(saved["authority_json"]) == ref
    stop(core, ref)
    activate(core, policy, revision=2, operation="replace-life-policy")
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_REVISION_CHANGED"):
        notices.validate(core.store, core.settings, saved, core.clock())
    assert core.store.one("SELECT authority_json FROM jobs WHERE job_id=?", (job_id,)) == {
        "authority_json": saved["authority_json"],
    }
    assert core.model.calls == []


def test_life_running_validation_uses_native_state_and_lease_not_supplied_state(core, tmp_path):
    life_authorized(core, tmp_path)
    job_id = due(core)
    job = core.claim_job("life-test", job_id=job_id)
    assert job
    with pytest.raises(CoreError, match="JOB_LEASE_LOST"):
        notices.validate(core.store, core.settings,
                         {**job, "state": "SUCCEEDED", "lease_token": "wrong"}, core.clock())
    notices.validate(core.store, core.settings, job, core.clock())


def test_life_paid_result_cannot_cross_owner_stop(core, tmp_path):
    _, ref = life_authorized(core, tmp_path)
    proposal(core)
    job_id = due(core)
    core.model.callback = lambda: stop(core, ref)
    assert run(core.run_once(job_id=job_id))
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    assert not core.store.rows("SELECT * FROM life_activity_results WHERE text<>''")
    assert core.store.rows("SELECT * FROM outbox") == []


@pytest.mark.parametrize("change", ["stop", "off", "restore"])
def test_owner_custody_cannot_fall_back_to_mock_life_scope(core, tmp_path, change):
    _, ref = life_authorized(core, tmp_path)
    proposal(core)
    assert run(core.run_once(job_id=due(core)))
    assert core.life_topics.preview(core.settings.owner_qq_id)["items"][0]["results"]
    if change == "stop":
        stop(core, ref)
    elif change == "restore":
        with core.store.transaction():
            owner_authority.restore(core.store)
    core.settings.mode = "mock"
    core.settings.owner_runtime = OwnerRuntimeConfig()
    assert owner_authority.required(core.store, core.settings)
    assert not core.life_topics.preview(core.settings.owner_qq_id)["items"][0]["results"]


def test_social_purpose_can_plan_but_cannot_execute_account_action(core, tmp_path):
    module, source, conversation = background(core, max_output_tokens=512)
    _, ref = authorize(core, tmp_path, account=False, module=module)
    result = planned(module, source)
    snapshot = core.store.one("SELECT input_snapshot_json FROM social_autonomy_plans")
    assert json.loads(snapshot["input_snapshot_json"])["authority"] == ref
    assert result["state"] == "READY" and len(module.model.calls) == 1
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_PURPOSE_REQUIRED"):
        queue(module, result, conversation)
    assert core.store.rows("SELECT * FROM outbox") == []


def test_social_stop_during_paid_request_discards_result_without_repayment(core, tmp_path):
    module, source, _ = background(core, max_output_tokens=512)
    policy, ref = authorize(core, tmp_path, module=module)
    module.model.callback = lambda: stop(core, ref)
    with pytest.raises(CoreError, match="OWNER_AUTHORITY"):
        planned(module, source)
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    assert core.store.one("SELECT state FROM social_autonomy_plans")["state"] == "REJECTED"
    assert core.store.rows("SELECT * FROM outbox") == []
    activate(core, policy, revision=2, operation="resume-after-social-stop")
    with pytest.raises(CoreError, match="SOCIAL_CANDIDATE_ALREADY_CONSIDERED"):
        planned(module, source, key="new-key-same-source")
    assert len(module.model.calls) == 1


def test_social_native_retry_keeps_authority_and_is_cancelled_after_revision(core, tmp_path):
    module, source, _ = background(core, max_output_tokens=512)
    core.settings.cloud_budget = CloudBudgetConfig(daily_micro_cny=0)
    policy, ref = authorize(core, tmp_path, module=module)
    with pytest.raises(CoreError, match="CLOUD_DAILY_BUDGET_EXHAUSTED"):
        planned(module, source)
    job = core.store.one("SELECT * FROM jobs WHERE kind='social_plan'")
    assert job and json.loads(job["authority_json"]) == ref
    core.clock.advance(61)
    stop(core, ref)
    activate(core, policy, revision=2, operation="revision-before-social-retry")
    assert social_plan_jobs.claim(module, "retry-worker") is None
    assert core.store.one("SELECT state FROM jobs WHERE job_id=?", (job["job_id"],)) == {
        "state": "CANCELLED",
    }
    assert module.model.calls == []


def test_native_outbox_pins_authority_and_stop_preserves_no_io_receipt(core, tmp_path):
    module, source, conversation = background(core, max_output_tokens=512)
    _, ref = authorize(core, tmp_path, module=module)
    frozen = queue(module, planned(module, source), conversation)
    token = uid("lease")
    # Native hook test. Shared transport/API migration has its own integration receipt.
    with core.store.transaction():
        core.store.execute("UPDATE outbox SET state='LEASED',lease_token=?,lease_until=?",
                           (token, after(core.clock(), 60)))
        view = account_actions.scope(core.store, core.settings, core.clock, ref,
                                     frozen.authorization_sha256, message_id=frozen.message_id)
        account_actions.lease_receipt(core.store, core.settings, core.clock,
                                      frozen.message_id, token, view)
    row = core.store.one("SELECT * FROM outbox")
    assert json.loads(row["authority_json"]) == ref
    stop(core, ref)
    with core.store.transaction(), pytest.raises(CoreError, match="OWNER_AUTHORITY"):
        module.validate_delivery(frozen.message_id, token)
    body = account_actions.CancelBeforeSend(
        authority=ref, message_id=frozen.message_id, lease_token=token,
        content_sha256=row["content_sha256"], payload_sha256=frozen.payload_sha256,
        reason="OWNER_STOPPED",
    )
    result = account_actions.cancel_before_send(core.store, core.settings, core.clock, body)
    assert result["state"] == "NOT_SENT"
    assert account_actions.cancel_before_send(core.store, core.settings, core.clock, body) == result
    assert len(module.model.calls) == 1


def test_account_dtos_reject_legacy_window_and_malformed_authority():
    base = dict(message_id="msg_one", lease_token="lease_one", content_sha256="a" * 64,
                payload_sha256="b" * 64)
    for extra in ({"window_sha256": "c" * 64}, {"authority": {}},
                  {"authority": {"version": True}}):
        with pytest.raises(ValidationError):
            account_actions.SendCheck(**base, **extra)


def test_owner_bound_unknown_plan_stays_charged_and_is_not_replayed(core, tmp_path):
    module, source, _ = background(core, max_output_tokens=512)
    authorize(core, tmp_path, module=module)
    module.model.error = TimeoutError("synthetic provider outcome unknown")
    with pytest.raises(TimeoutError):
        planned(module, source)
    usage = core.store.one("SELECT * FROM model_usage")
    assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] > 0
    assert planned(module, source)["state"] == "REJECTED"
    assert core.store.one("SELECT * FROM model_usage") == usage
    assert len(module.model.calls) == 1 and core.store.rows("SELECT * FROM outbox") == []


def test_owner_bound_source_erasure_during_model_keeps_cost_and_removes_draft(core, tmp_path):
    module, source, _ = background(core, max_output_tokens=512)
    authorize(core, tmp_path, module=module)

    def erase():
        with core.store.transaction():
            core.ledger.expire_events(core.store, [source], core.clock())

    module.model.callback = erase
    with pytest.raises(CoreError):
        planned(module, source)
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    assert core.store.one("SELECT state,redacted FROM social_autonomy_plans") == {
        "state": "CANCELLED", "redacted": 1,
    }
    assert core.store.rows("SELECT * FROM outbox") == []


@pytest.mark.parametrize("allow_affect", [False, True])
def test_social_and_life_do_not_read_feedback_without_affect_purpose(
    core, tmp_path, monkeypatch, allow_affect,
):
    from companion.life_topics import _paused
    from companion.social import proactive_allowed
    from tests.test_social_autonomy import service

    core.settings.affect_enabled = core.settings.proactive_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    ingest(core, text="目前不想聊")
    assert core.store.one("SELECT new_state FROM affect_evidence") == {"new_state": "pause"}
    core.store.execute("UPDATE jobs SET state='CANCELLED'")
    service(core)
    authorize(core, tmp_path, account=False,
              additional_purposes=["proactive", *(["affect"] if allow_affect else [])])
    calls = []
    check_contact = AffectEngine.contact_paused

    def observed_contact(engine, source_ids=()):
        calls.append(tuple(source_ids))
        return check_contact(engine, source_ids)

    monkeypatch.setattr(AffectEngine, "contact_paused", observed_contact)
    assert proactive_allowed(core.store, core.settings, core.clock()) is (not allow_affect)
    assert not _paused(core.store, core.settings, core.clock())
    assert calls == ([()] if allow_affect else [])
    core.clock.advance(core.settings.affect_decay_minutes * 60 + 1)
    assert proactive_allowed(core.store, core.settings, core.clock())
    assert core.model.calls == []
