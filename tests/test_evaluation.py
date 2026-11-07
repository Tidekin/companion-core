"""No external I/O: finite experiment admission shares the original Core budget."""

import asyncio
import copy
import hashlib
import json

import httpx
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import budget, cloud_budget, evaluation, owner_authority, owner_input, paid_budget
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.model import ModelClient, ModelResponse, model_payload
from companion.policy import ConsentRequest, ConsentService
from companion.qq_rich import input_digest
from companion.runtime import Runtime
from companion_delivery_protocol.authority import capture_signing_payload
from tests.conftest import event, ingest
from tests.support.paid_model import approved_live


def prepare(core, tmp_path, *, chat=False, **limits):
    source = None if chat else ingest(core, text="SYNTHETIC private source")
    if source:
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?", (source["job_id"],))
    core.settings.evaluation_enabled = True
    core.settings.max_output_tokens = 2048
    approved_live(core, tmp_path, provider="deepseek", model="deepseek-flash",
                  max_output_tokens=2048, input_rate=300000, cached_input_rate=6000,
                  output_rate=1200000, **limits)
    from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
    from tests.support.owner_authority import activate

    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    key = Ed25519PrivateKey.generate() if chat else None
    core.evaluation_test_key = key
    activate(core, OwnerGrantPolicy(
        owner_qq_id=core.settings.owner_qq_id,
        purposes=["evaluation", "chat"] if chat else ["evaluation"],
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capture={"public_key": key.public_key().public_bytes_raw().hex(),
                 "identity_sha256": paid_budget.sha(
                     [core.settings.owner_qq_id, core.settings.bot_qq_id]),
                 "protocol_epoch": "evaluation-chat-test"} if key else None,
    ))
    if chat:
        source = admitted_chat(core, 1, text="SYNTHETIC private source")
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?", (source["job_id"],))
    rows = []
    for repetition in range(3):
        for strategy in (("baseline", "participation_v1") if repetition % 2 == 0 else (
            "participation_v1", "baseline")
        ):
            messages = [{"role": "system", "content": "Synthetic " + strategy},
                        {"role": "user", "content": "SYNTHETIC private source"}]
            payload = model_payload(core.settings, messages, "evaluation")
            rows.append(dict(case="synthetic", repetition=repetition, strategy=strategy,
                             payload=payload, payload_sha256=paid_budget.sha(payload),
                             shared_context_sha256=paid_budget.sha(messages[1:]),
                             planned_max_output_tokens=2048))
    plan = dict(requests=rows, request_count=6, case_count=1, provider="deepseek",
                model="deepseek-flash", actual_provider_calls=0, actual_cost_micro_usd=0,
                status="PREPARED_NO_MODEL_CALLS", rates_micro_usd_per_million={
                    "input": 300000, "cached_input": 6000, "output": 1200000})
    plan["plan_sha256"] = paid_budget.sha(plan)
    return evaluation.Manifest(plan=plan, source_event_ids=[source["event_id"]],
                               baseline_sha256="a" * 64, source_document_sha256="b" * 64)


def admitted_chat(core, number=2, *, text="synthetic independent chat"):
    from companion.owner_authority_protocol import OwnerCapture

    view = owner_authority.current(core.store, core.settings, core.clock, purpose="chat")
    source = (f"{core.settings.bot_qq_id}:onebot-direct:FriendMessage:"
              f"{core.settings.owner_qq_id}:epoch:evaluation-chat-test:{number}")
    event_id = "evt_" + hashlib.sha256(source.encode()).hexdigest()
    value = dict(authority=view.authority.as_dict(), kind="authenticated_onebot_ws",
                 source_event_id=source, event_id=event_id,
                 input_sha256=input_digest({"text": text}), frame_sha256="b" * 64,
                 evidence_sha256="c" * 64, identity_sha256=view.policy.capture.identity_sha256,
                 protocol_epoch=view.policy.capture.protocol_epoch,
                 occurred_at=core.clock(), received_at=core.clock())
    value["signature"] = core.evaluation_test_key.sign(capture_signing_payload(value)).hex()
    capture = OwnerCapture.model_validate(value)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    incoming = event(core, number, text, origin="onebot-direct:FriendMessage:10001").model_copy(
        update={"event_id": event_id, "source_event_id": source})
    return core.ingest(incoming, "evaluation-chat-" + str(number))


def authorize(core, manifest):
    view = evaluation.preview(core.store, core.settings, core.clock, manifest)
    consent = ConsentService(core.store, core.settings, core.clock).grant(
        ConsentRequest(**view["consent_scope"], duration_seconds=600))
    return evaluation.Submission(manifest=manifest, consent_id=consent["consent_id"])


def response(request):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "SYNTHETIC observed reply"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 2}})


def work(core, handler=response, hooks=None):
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False,
                                     event_hooks=hooks or {}) as client:
            core.model = ModelClient(core.settings, client)
            return await core.run_once("evaluation-test")
    return asyncio.run(execute())


def result(core, submitted):
    return evaluation.results(core.store, core.settings, core.clock, submitted["evaluation_id"])


def test_six_frozen_payloads_use_native_job_usage_cloud_and_no_delivery(core, tmp_path):
    manifest = prepare(core, tmp_path)
    request = authorize(core, manifest)
    submitted = evaluation.submit(core, request)
    before_memories = core.store.rows("SELECT * FROM memories")
    wire = []
    work(core, lambda req: (wire.append(paid_budget.sha(json.loads(req.content))) or response(req)))
    view = result(core, submitted)
    assert view["state"] == "SUCCEEDED", view["error_code"]
    assert wire == [r["payload_sha256"] for r in manifest.plan["requests"]]
    assert len(view["requests"]) == 6 and all(r["complete_response"] for r in view["requests"])
    assert core.store.one("SELECT SUM(billed_micro_usd) n FROM model_usage")["n"] == 72
    assert cloud_budget.summary(core.store, core.settings, core.clock())["used_micro_cny"] == 576
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.store.rows("SELECT * FROM memories") == before_memories
    assert evaluation.submit(core, request)["reused"]
    core.store.close()
    with Core(core.settings, core.model, core.clock) as restarted:
        assert not work(restarted)
    assert len(wire) == 6


def test_preview_does_not_authorize_or_reserve(core, tmp_path):
    manifest = prepare(core, tmp_path)
    evaluation.preview(core.store, core.settings, core.clock, manifest)
    assert core.store.rows("SELECT * FROM consents") == []
    assert core.store.rows("SELECT * FROM model_usage") == []
    with pytest.raises(CoreError, match="CONSENT_REQUIRED"):
        evaluation.submit(core, evaluation.Submission(manifest=manifest, consent_id="missing"))


def test_original_revocation_remains_available_before_evaluation_migration(core):
    core.store.execute("DROP TABLE evaluation_results")
    core.store.execute("DROP TABLE evaluation_runs")
    core.settings.camera_enabled = True
    service = ConsentService(core.store, core.settings, core.clock)
    consent = service.grant(ConsentRequest(capabilities=["camera.observe"],
                                           device_id="synthetic-camera", session_id="old-scope"))
    assert service.revoke(consent["consent_id"])["revoked"]
    evaluation.stop_window(core.store, "f" * 64, core.clock())
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_expired_source_is_not_reauthorized_by_fresh_consent(core, tmp_path):
    from companion.store import after

    manifest = prepare(core, tmp_path)
    core.store.execute("UPDATE events SET ingested_at=? WHERE event_id=?", (
        after(core.clock(), -86400 * (core.settings.raw_event_retention_days + 1)),
        manifest.source_event_ids[0]))
    with pytest.raises(CoreError, match="SOURCE_EXPIRED"):
        authorize(core, manifest)
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_preexisting_cumulative_liability_blocks_evaluation(core, tmp_path):
    from companion import owner_authority
    from companion.owner_authority_protocol import OwnerGrantPolicy
    from tests.support.owner_authority import activate
    from tests.test_cumulative_budget import policy

    manifest = prepare(core, tmp_path, chat=True)
    policy(core, account={"model_attempts": 1})
    prior_policy = OwnerGrantPolicy.model_validate_json(
        owner_authority._latest(core.store)["policy_json"])
    activate(core, prior_policy.model_copy(update={
        "paid_contract_sha256": paid_budget.sha(paid_budget.snapshot(core.settings)),
    }), revision=1, operation="operation-cumulative-revision")
    normal = admitted_chat(core)
    assert core.claim_job("prior-cumulative-chat", job_id=normal["job_id"])
    prior = budget.reserve(core.store, core.settings, normal["job_id"], "chat",
                           [{"role": "user", "content": "prior responsibility"}], core.clock(),
                           clock=core.clock)
    budget.settle(core.store, core.settings, prior, ModelResponse("old", 30, 2))
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?", (normal["job_id"],))
    submitted = evaluation.submit(core, authorize(core, manifest))
    calls = []
    work(core, lambda req: (calls.append(1) or response(req)))
    assert calls == [] and result(core, submitted)["error_code"] == "CUMULATIVE_LIMIT_EXHAUSTED"
    assert core.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == 1


@pytest.mark.parametrize("condition", ["revoked", "expired", "source", "window", "disabled",
                                       "new_owner"])
def test_changes_block_execution_without_io(core, tmp_path, condition):
    manifest = prepare(core, tmp_path, chat=condition == "new_owner")
    request = authorize(core, manifest)
    submitted = evaluation.submit(core, request)
    if condition == "revoked":
        ConsentService(core.store, core.settings, core.clock).revoke(request.consent_id)
    elif condition == "expired":
        core.clock.advance(601)
    elif condition == "source":
        core.store.execute("UPDATE events SET redacted=1 WHERE event_id=?",
                           (manifest.source_event_ids[0],))
    elif condition == "window":
        core.settings.paid_model = core.settings.paid_model.model_copy(
            update={"window_id": "different-window"})
    elif condition == "disabled":
        core.settings.evaluation_enabled = False
    else:
        newer = admitted_chat(core)
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?", (newer["job_id"],))
    calls = []
    work(core, lambda req: (calls.append(1) or response(req)))
    assert calls == []
    assert core.store.one("SELECT state FROM jobs WHERE job_id=?", (submitted["job_id"],))[
        "state"] in {"FAILED", "CANCELLED"}


@pytest.mark.parametrize("failure", ["timeout", "usage_missing", "amount_overrun"])
def test_unknown_stops_remaining_calls_and_never_retries(core, tmp_path, failure):
    manifest = prepare(core, tmp_path)
    submitted = evaluation.submit(core, authorize(core, manifest))
    calls = []
    def handler(request):
        calls.append(1)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        body = response(request).json()
        if failure == "usage_missing":
            del body["usage"]
        else:
            body["usage"]["prompt_tokens"] = 17000  # $0.0051 exceeds experiment attempt bound.
        return httpx.Response(200, json=body)
    work(core, handler)
    assert len(calls) == 1
    assert core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"
    assert all(r["state"] == "NOT_STARTED" for r in result(core, submitted)["requests"][1:])
    assert not work(core, handler)
    assert len(calls) == 1


def test_normal_task_spend_competes_for_same_cloud_ceiling(core, tmp_path):
    manifest = prepare(core, tmp_path, chat=True)
    cap = core.settings.cloud_budget.model_copy(update={"daily_micro_cny": 30000})
    core.settings.cloud_budget = cap
    # Native preceding charge; no evaluation-local balance can discard this liability.
    normal = admitted_chat(core)
    assert core.claim_job("prior-cloud-chat", job_id=normal["job_id"])
    prior = budget.reserve(core.store, core.settings, normal["job_id"], "chat",
                           [{"role": "user", "content": "prior"}], core.clock(), clock=core.clock)
    budget.settle(core.store, core.settings, prior, ModelResponse("old", 30, 2048))
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?", (normal["job_id"],))
    submitted = evaluation.submit(core, authorize(core, manifest))
    calls = []
    work(core, lambda req: (calls.append(1) or response(req)))
    assert calls == []
    assert result(core, submitted)["state"] == "FAILED"
    assert result(core, submitted)["error_code"] == "CLOUD_DAILY_BUDGET_EXHAUSTED"


def test_source_revoke_during_transport_hook_fences_request(core, tmp_path):
    manifest = prepare(core, tmp_path)
    request = authorize(core, manifest)
    evaluation.submit(core, request)
    calls = []
    async def revoke(_request):
        ConsentService(core.store, core.settings, core.clock).revoke(request.consent_id)
    work(core, lambda req: (calls.append(1) or response(req)), hooks={"request": [revoke]})
    assert calls == []
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
        "state": "SETTLED", "billed_micro_usd": 0}
    assert core.store.one("SELECT 1 FROM audit_log WHERE action='model.not_dispatched'")


def test_crash_after_settlement_before_result_never_repays(core, tmp_path):
    manifest = prepare(core, tmp_path)
    submitted = evaluation.submit(core, authorize(core, manifest))
    job = core.claim_job("crashed-worker")
    item = manifest.plan["requests"][0]
    rid = evaluation.request_id(submitted["manifest_sha256"], 0)
    budget.reserve(core.store, core.settings, job["job_id"], "evaluation",
                   item["payload"]["messages"], core.clock(), request_id=rid,
                   logical_request_id=rid, clock=core.clock)
    budget.settle(core.store, core.settings, rid, ModelResponse("lost response", 30, 2))
    core.clock.advance(core.settings.lease_seconds + 1)
    core.store.close()
    with Core(core.settings, core.model, core.clock) as restarted:
        assert not work(restarted)
        assert result(restarted, submitted)["state"] == "CANCELLED"
        assert restarted.store.one("SELECT COUNT(*) n FROM model_usage")["n"] == 1


def test_expired_lease_keeps_unknown_in_original_journal(core, tmp_path):
    manifest = prepare(core, tmp_path)
    submitted = evaluation.submit(core, authorize(core, manifest))
    job = core.claim_job("crashed-worker")
    rid = evaluation.request_id(submitted["manifest_sha256"], 0)
    budget.reserve(core.store, core.settings, job["job_id"], "evaluation",
                   manifest.plan["requests"][0]["payload"]["messages"], core.clock(),
                   request_id=rid, logical_request_id=rid, clock=core.clock)
    core.clock.advance(core.settings.lease_seconds + 1)
    assert not work(core)
    assert core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"
    with core.store.transaction():
        paid_budget.journal(core.store, core.settings.paid_model, allow_settlement=True,
                            initialize=False)


def test_erasure_and_old_backup_replay_remove_inputs_results_keep_cost(core, tmp_path):
    manifest = prepare(core, tmp_path)
    submitted = evaluation.submit(core, authorize(core, manifest))
    work(core)
    archived, restored = tmp_path / "old.bin", tmp_path / "restored.sqlite3"
    key = Fernet.generate_key()
    backup(core.settings.database, archived, key)
    with core.store.transaction():
        core.ledger.expire_events(core.store, manifest.source_event_ids, core.clock())
    assert core.store.one("SELECT manifest_json FROM evaluation_runs")["manifest_json"] == "{}"
    assert core.store.rows("SELECT * FROM evaluation_results") == []
    assert core.store.one("SELECT SUM(billed_micro_usd) n FROM model_usage")["n"] == 72
    restore(archived, restored, key, core.settings.erasure_ledger)
    restored_settings = core.settings.model_copy(update={"database": str(restored)})
    with Runtime(restored_settings, core.clock) as runtime:
        assert runtime.store.one("SELECT redacted FROM evaluation_runs")["redacted"] == 1
        assert runtime.store.rows("SELECT * FROM evaluation_results") == []
        assert runtime.store.one("SELECT SUM(billed_micro_usd) n FROM model_usage")["n"] == 72
    with pytest.raises(CoreError, match="ERASED"):
        result(core, submitted)


def test_tampered_plan_cannot_reuse_exact_manifest_consent(core, tmp_path):
    manifest = prepare(core, tmp_path)
    request = authorize(core, manifest)
    changed = copy.deepcopy(manifest)
    changed.plan["requests"][0]["payload"]["messages"][0]["content"] = "different"
    with pytest.raises(CoreError, match="MANIFEST_INVALID"):
        evaluation.submit(core, evaluation.Submission(
            manifest=changed, consent_id=request.consent_id))
    changed = manifest.model_copy(update={"baseline_sha256": "c" * 64})
    with pytest.raises(CoreError, match="CONSENT_REQUIRED"):
        evaluation.submit(core, evaluation.Submission(
            manifest=changed, consent_id=request.consent_id))


def test_redacted_model_text_is_not_presented_as_complete_original_response(core, tmp_path):
    manifest = prepare(core, tmp_path)
    submitted = evaluation.submit(core, authorize(core, manifest))
    def handler(request):
        body = response(request).json()
        body["choices"][0]["message"]["content"] = "Synthetic secret synthetic-key"
        return httpx.Response(200, json=body)
    work(core, handler)
    observed = result(core, submitted)
    assert observed["state"] == "SUCCEEDED"
    assert all(row["text_redacted"] and not row["complete_response"]
               for row in observed["requests"])
    assert "synthetic-key" not in json.dumps(observed)


def test_control_api_rejects_bridge_token_and_does_not_echo_invalid_input(core, tmp_path):
    from pydantic import SecretStr

    from companion.api import create_app

    manifest = prepare(core, tmp_path)
    core.settings.control_token = SecretStr("synthetic-control-" * 3)
    app = create_app(core.settings)
    app.state.core = core
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            bridge = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
            denied = await client.post("/internal/v2/evaluations/preview",
                                       json=manifest.model_dump(), headers=bridge)
            assert denied.status_code == 401
            denied_read = await client.get("/internal/v2/evaluations/eval_missing", headers=bridge)
            assert denied_read.status_code == 401
            headers = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
            preview = await client.post("/internal/v2/evaluations/preview",
                                        json=manifest.model_dump(), headers=headers)
            assert preview.status_code == 200, preview.text
            bad = await client.post("/internal/v2/evaluations/preview", headers=headers,
                                    json={"plan": "PRIVATE-INVALID-TEXT"})
            assert bad.status_code == 400 and "PRIVATE-INVALID-TEXT" not in bad.text
    asyncio.run(scenario())


@pytest.mark.parametrize("treatment", ["participation_v2", "participation_v1", "participation_v3"])
def test_native_plan_accepts_only_the_declared_v2_treatment(core, tmp_path, treatment):
    manifest = prepare(core, tmp_path)
    plan = {key: value for key, value in manifest.plan.items() if key != "plan_sha256"}
    for row in plan["requests"]:
        if row["strategy"] == "participation_v1":
            row["strategy"] = "participation_v2"
    plan["treatment"] = treatment
    plan["plan_sha256"] = paid_budget.sha(plan)
    changed = manifest.model_copy(update={"plan": plan})
    if treatment == "participation_v2":
        evaluation.preview(core.store, core.settings, core.clock, changed)
    else:
        with pytest.raises(CoreError, match="MANIFEST_INVALID"):
            evaluation.preview(core.store, core.settings, core.clock, changed)
