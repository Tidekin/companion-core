"""An explicit legacy renewal preserves financial evidence and every resource limit."""

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from companion import budget, meme_service, model_carry, owner_authority, owner_input, paid_budget
from companion.contracts import CoreError
from companion.monetary_retirement import PAID_FIELDS
from companion.owner_authority_protocol import AuthorityStop
from companion.qq_rich import input_digest
from companion.store import encode, utcnow
from companion_delivery_protocol import digest
from tests import model_carry_support
from tests.conftest import run
from tests.model_carry_support import MESSAGES, executors, migrated, observer
from tests.owner_model_fixtures import authorized_job
from tests.support.core_http import real_core_http
from tests.support.model_carry_native import child, finished
from tests.support.owner_authority import activate
from tests.support.worker_wakeup_process import (
    close_child,
    finish,
    ready,
    signed_input,
    start,
    state,
)


@pytest.fixture
def legacy(core, tmp_path, monkeypatch, request):
    core.settings.expression_enabled = True
    core.settings.memory_enabled = True
    core.settings.rem = core.settings.rem.model_copy(update={"enabled": True})
    snapshot = paid_budget.snapshot
    capabilities = owner_authority.capability_hashes

    def old_capabilities(settings):
        return {
            "meme": {**meme_service.policy_value(settings), "meme_caption_daily_micro_usd": 1000},
            "rem": {**settings.rem.model_dump(mode="json"),
                    "daily_micro_usd": 2000, "monthly_micro_usd": 60000},
        }

    def old_snapshot(settings):
        return {**snapshot(settings), **{key: 1000000 for key in PAID_FIELDS},
                "scenario_daily_micro_usd": {"chat": 1000000}}

    with monkeypatch.context() as old:
        old.setattr(paid_budget, "snapshot", old_snapshot)
        old.setattr(owner_authority, "capability_hashes", lambda settings: {
            **capabilities(settings),
            **{key: digest(value) for key, value in old_capabilities(settings).items()},
        })
        old.setattr(model_carry_support, "authorized_job", lambda core, *, purposes: authorized_job(
            core, purposes=(*purposes, "memory", "meme", "rem"),
        ))
        case = migrated(core, tmp_path, expires_at=None, rates_valid_until=None,
                        window_calls=getattr(request, "param", 1))
        previous = old_snapshot(core.settings)
        evidence = old_capabilities(core.settings)
    before = {
        table: core.store.rows("SELECT * FROM " + table + " ORDER BY request_id")
        for table in ("model_usage", "model_admissions")
    }
    journal = Path(core.settings.paid_model.journal_path)
    prefix = journal.read_bytes()
    authority = case.successor["authority"]
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="stop-for-money-retirement", authority=authority,
    ))
    core.settings.paid_model = core.settings.paid_model.model_copy(update={
        "window_id": "retired-money-window", "approval_sha256": "1" * 64,
    })
    items = executors(core.clock())
    witness = model_carry_support.verify(items, observe=observer(items))
    with core.store.transaction():
        view = owner_authority.custody(core.store, core.settings, settlement=True)
        manifest = model_carry.manifest(core.store, core.settings, authority,
                                        core.settings.paid_model.window_id, items, view)
    policy = case.policy.model_copy(update={
        "authorization_sha256": "2" * 64, "unknown_carry_sha256": digest(manifest),
        "paid_contract_sha256": digest(snapshot(core.settings)),
        "capability_policies": {key: capabilities(core.settings)[key]
                                for key in case.policy.capability_policies},
    })
    request = {
        "version": 2, "operation_id": "retire-original-money-contract",
        "authorization_sha256": policy.authorization_sha256,
        "expected_head_sha256": view["head_sha256"], "authority": authority,
        "successor_window_id": core.settings.paid_model.window_id,
        "manifest_sha256": digest(manifest), "successor_policy": policy.model_dump(mode="json"),
        "executors": items, "previous_paid_contract": previous,
        "monetary_retirement": evidence,
    }
    return SimpleNamespace(case=case, before=before, request=request, policy=policy,
                           witness=witness, journal=journal, prefix=prefix)


@pytest.mark.parametrize("legacy", [3], indirect=True)
def test_retirement_uses_native_cli_http_and_independent_worker(core, tmp_path, legacy):
    core.clock.now = datetime.fromisoformat(utcnow())
    core.settings.control_token = SecretStr("synthetic-money-retirement-control")

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as http:
            path = tmp_path / "retirement-request.json"
            raw = encode(legacy.request).encode()
            path.write_bytes(raw)
            controller = await child(
                tmp_path, "retirement-controller", core, mode="controller",
                command="carry-closed-model-attempts", executors=legacy.witness.value,
                request=str(path), request_sha256=hashlib.sha256(raw).hexdigest(),
            )
            receipt = json.loads(await finished(controller))
            assert receipt["kind"] == "owner_model_approval_renewal_v1"
            control = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
            query = {"expected_revision": 4, "policy": legacy.policy.model_dump(mode="json")}
            preview = await http.post("/internal/v1/owner-authority/preview",
                                      headers=control, json=query)
            assert preview.status_code == 200, preview.text
            active = await http.post("/internal/v1/owner-authority/activate", headers=control,
                json={**query, "operation_id": "native-money-retirement",
                      "preview_sha256": preview.json()["preview_sha256"]})
            assert active.status_code == 200, active.text
            template = legacy.case.capture.model_copy(
                update={"authority": active.json()["authority"],
                        "input_sha256": input_digest({"text": "synthetic owner input"})},
            )
            worker = await start(core, tmp_path / "retirement-worker", duration=30, controlled=True)
            try:
                await ready(worker)
                capture, message = signed_input(core, template, legacy.case.key, 2)
                approved = await http.post("/internal/v1/owner-authority/inputs/approve",
                                           json=capture.model_dump(mode="json"))
                assert approved.status_code == 200, approved.text
                response = await http.post("/internal/v1/inbox/events",
                    json=message.model_dump(mode="json"),
                    headers={"X-Idempotency-Key": "money-retirement-new-input"})
                assert response.status_code == 202, response.text
                await state(core, response.json()["job_id"])
                (tmp_path / "retirement-worker" / "release").touch()
                assert (await finish(worker))["total"]["provider_calls"] == 1
            finally:
                await close_child(worker)
            assert core.store.one("SELECT * FROM model_usage WHERE request_id=%s",
                                   (legacy.case.request_id,)) == legacy.before["model_usage"][0]

    run(scenario())


def test_original_money_contract_can_retire_without_resetting_calls_or_unknown(core, legacy):
    value = legacy
    receipt = model_carry.prepare(core.store, core.settings, value.request,
                                  value.witness, core.clock())
    assert receipt["kind"] == "owner_model_approval_renewal_v1"
    assert model_carry.prepare(core.store, core.settings, value.request,
                               value.witness, core.clock()) == receipt
    active, _ = activate(core, value.policy, revision=4, operation="activate-money-retirement")
    owner_authority.current(core.store, core.settings, core.clock, purpose="chat")
    assert value.journal.read_bytes().startswith(value.prefix)
    for table, before in value.before.items():
        assert core.store.rows("SELECT * FROM " + table + " ORDER BY request_id") == before
    assert value.before["model_usage"][0]["state"] == "UNKNOWN"
    with pytest.raises(CoreError):
        owner_input.approve(core.store, core.settings, core.clock, value.case.capture)
    core.authorized_model_test_capture = active, value.case.key
    core.authorized_model_test_jobs = {}
    job = authorized_job(core, number=2)
    with pytest.raises(CoreError, match="MODEL_WINDOW_BUDGET_EXHAUSTED"):
        budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())
    changed = copy.deepcopy(value.request)
    changed["monetary_retirement"]["rem"]["max_source_memories"] = 1
    with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_CHANGED"):
        model_carry.prepare(core.store, core.settings, changed, value.witness, core.clock())


@pytest.mark.parametrize("change", [
    "missing_proof", "forged_rem", "forged_meme", "missing_flag", "missing_paid_field",
    "changed_original_amount", "extra_capability", "window_calls", "concurrency",
    "output_tokens", "rem_scope", "purpose",
])
def test_retirement_rejects_missing_evidence_and_nonfinancial_changes(core, legacy, change):
    request = copy.deepcopy(legacy.request)
    if change == "missing_proof":
        del request["monetary_retirement"]["rem"]
    elif change == "forged_rem":
        request["monetary_retirement"]["rem"]["daily_micro_usd"] += 1
    elif change == "forged_meme":
        value = request["monetary_retirement"]["meme"]
        value["discovery"] = not value["discovery"]
    elif change == "missing_flag":
        del request["monetary_retirement"]
    elif change == "missing_paid_field":
        del request["previous_paid_contract"]["daily_micro_usd"]
    elif change == "changed_original_amount":
        request["previous_paid_contract"]["daily_micro_usd"] += 1
    elif change == "extra_capability":
        request["monetary_retirement"]["voice"] = {}
    elif change in {"window_calls", "concurrency", "output_tokens"}:
        field = "max_output_tokens" if change == "output_tokens" else change
        core.settings.paid_model = core.settings.paid_model.model_copy(update={
            field: getattr(core.settings.paid_model, field) + 1,
        })
        request["successor_policy"]["paid_contract_sha256"] = digest(
            paid_budget.snapshot(core.settings),
        )
    elif change == "rem_scope":
        core.settings.rem = core.settings.rem.model_copy(update={"max_source_memories": 2})
        request["successor_policy"]["capability_policies"]["rem"] = digest(
            core.settings.rem.model_dump(mode="json"),
        )
    else:
        request["successor_policy"]["purposes"].append("evaluation")
    prefix = legacy.journal.read_bytes()
    with pytest.raises(CoreError):
        model_carry.prepare(core.store, core.settings, request, legacy.witness, core.clock())
    assert legacy.journal.read_bytes() == prefix
    for table, before in legacy.before.items():
        assert core.store.rows("SELECT * FROM " + table + " ORDER BY request_id") == before
