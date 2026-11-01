"""Private REM approval reuses carried liabilities and the native authority chain."""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from companion import model_carry, owner_authority, paid_budget, rem
from companion.contracts import Candidate, CoreError
from companion.owner_authority_protocol import AuthorityStop, OwnerGrantPolicy
from companion.rem_protocol import RemConfig
from tests import owner_model_fixtures
from tests.conftest import run
from tests.model_carry_support import migrated
from tests.owner_model_fixtures import authorized_job
from tests.test_evaluation_process import process_config
from tests.test_model_carry_lifecycle import current_unknown, stop_current
from tests.test_model_renewal import renewal
from tests.test_onebot_capture import real_core_http
from tests.test_owner_authority import activate


def carried_memory(core, tmp_path, monkeypatch, *, memory=True):
    def policy(**values):
        return OwnerGrantPolicy(**{**values,
            "purposes": ["chat", "memory", "evaluation"] if memory else ["chat", "evaluation"],
            "memory_scope": "owner_private" if memory else "none"})

    core.settings.memory_enabled = True
    core.settings.rem = RemConfig(local_hour=0, max_output_tokens=128)
    with monkeypatch.context() as patch:
        patch.setattr(owner_model_fixtures, "OwnerGrantPolicy", policy)
        return migrated(core, tmp_path)


def rem_policy(core, old):
    return old.model_copy(update={
        "purposes": [*old.purposes, "rem"], "authorization_sha256": "a" * 64,
        "capability_policies": {**old.capability_policies,
            "rem": paid_budget.sha(core.settings.rem.model_dump(mode="json"))}})


def test_rem_revision_and_later_paid_renewal_preserve_original_unknown(core, tmp_path, monkeypatch):
    case = carried_memory(core, tmp_path, monkeypatch)
    before = core.store.rows("SELECT * FROM model_usage")
    stop_current(core, case)
    policy = rem_policy(core, case.policy)
    active, _ = activate(core, policy, revision=4, operation="approve-private-rem")
    assert owner_authority.current(core.store, core.settings, core.clock,
                                   purpose="rem").authority.as_dict() == active["authority"]
    assert core.store.rows("SELECT * FROM model_usage") == before
    case.policy, case.successor = policy, active
    request, renewed, witness = renewal(core, case)
    receipt = model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    assert receipt["retained_micro_usd"] == before[0]["billed_micro_usd"]
    activate(core, renewed, revision=6, operation="renew-after-rem-approval")
    assert owner_authority.current(core.store, core.settings, core.clock,
                                   purpose="rem").policy.capability_policies == (
                                       policy.capability_policies)
    assert core.store.rows("SELECT * FROM model_usage") == before
    assert not core.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("change", ["memory", "hash", "disabled", "authorization", "purpose",
                                    "other_capability", "retention", "paid"])
def test_rem_revision_cannot_expand_other_scope_or_change_accounting(
    core, tmp_path, monkeypatch, change,
):
    case = carried_memory(core, tmp_path, monkeypatch, memory=change != "memory")
    stop_current(core, case)
    policy = rem_policy(core, case.policy)
    if change == "hash":
        policy = policy.model_copy(update={"capability_policies": {"rem": "0" * 64}})
    elif change == "disabled":
        core.settings.rem = RemConfig(enabled=False)
    elif change == "authorization":
        policy = policy.model_copy(update={
            "authorization_sha256": case.policy.authorization_sha256})
    elif change == "purpose":
        policy = policy.model_copy(update={"purposes": [*policy.purposes, "goals"]})
    elif change == "other_capability":
        policy = policy.model_copy(update={"purposes": [*policy.purposes, "image"],
            "capability_policies": {**policy.capability_policies, "image": "0" * 64}})
    elif change == "retention":
        policy = policy.model_copy(update={"source_retention_days": 30})
    elif change == "paid":
        core.settings.paid_model = core.settings.paid_model.model_copy(update={"window_calls": 999})
    journal = Path(core.settings.paid_model.journal_path)
    before, finance = journal.read_bytes(), core.store.rows("SELECT * FROM model_usage")
    with pytest.raises(CoreError):
        activate(core, policy, revision=4, operation="reject-rem-scope-drift")
    assert journal.read_bytes() == before
    assert core.store.rows("SELECT * FROM model_usage") == finance
    assert owner_authority._latest(core.store)["state"] == "STOPPED"


def test_rem_keeps_current_input_uncertainty_fenced_until_normal_handoff(
    core, tmp_path, monkeypatch,
):
    case = carried_memory(core, tmp_path, monkeypatch)
    request_id = current_unknown(core)
    stop_current(core, case)
    before = core.store.rows("SELECT * FROM model_usage")
    policy = rem_policy(core, case.policy)
    with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_NOT_QUIESCENT"):
        activate(core, policy, revision=4, operation="reject-rem-current-unknown")
    same, _ = activate(core, case.policy, revision=4, operation="normal-closed-unknown-handoff")
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="rem-after-normal-handoff-stop", authority=same["authority"]))
    active, _ = activate(core, policy, revision=6, operation="rem-after-normal-handoff")
    assert active["authority"]["revision"] == 7
    assert core.store.rows("SELECT * FROM model_usage") == before
    assert core.store.one("SELECT state FROM model_usage WHERE request_id=?",
                          (request_id,))["state"] == "UNKNOWN"


def test_http_rem_revision_reaches_independent_native_worker(core, tmp_path, monkeypatch):
    case = carried_memory(core, tmp_path, monkeypatch)
    core.settings.control_token = SecretStr("synthetic-rem-revision-control")
    source_job = authorized_job(core, number=2)
    source = core.store.one("SELECT event_id FROM jobs WHERE job_id=?", (source_job,))["event_id"]
    with core.store.transaction():
        core.memory.add(Candidate(subject="user", kind="fact", fact_key="private-rem-material",
            content="synthetic model boundary source",
            evidence_quote="synthetic model boundary source",
            evidence_event_id=source), core.clock(), confirmed=True)
    stop_current(core, case)
    original = core.store.rows("SELECT * FROM model_usage")
    policy = rem_policy(core, case.policy)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as http:
            query = {"expected_revision": 4, "policy": policy.model_dump(mode="json")}
            denied = await http.post("/internal/v1/owner-authority/preview", json=query)
            assert denied.status_code == 401
            headers = {"Authorization": "Bearer synthetic-rem-revision-control"}
            preview = await http.post("/internal/v1/owner-authority/preview", json=query,
                                      headers=headers)
            assert preview.status_code == 200, preview.text
            active = await http.post("/internal/v1/owner-authority/activate", headers=headers,
                json={**query, "operation_id": "native-rem-purpose-revision",
                      "preview_sha256": preview.json()["preview_sha256"]})
            assert active.status_code == 200, active.text
            job = rem.schedule_due(core)
            assert job
            config, log = tmp_path / "rem-worker.json", tmp_path / "rem-provider.jsonl"
            process_config(core, config)
            process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
                "tests.rem_process", "--config", str(config), "--now", core.clock(),
                "--job", job, "--log", str(log), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            try:
                output, error = await asyncio.wait_for(process.communicate(), 45)
                assert process.returncode == 0, (output, error)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            assert len(log.read_text("utf-8").splitlines()) == 1
            assert rem.records(core)[0]["state"] == "SUCCEEDED"
            assert rem.records(core)[0]["decision"] == "SKIP"
            assert core.store.rows("SELECT * FROM model_usage WHERE scenario<>'rem'") == original
            assert not core.store.rows("SELECT * FROM outbox")

    run(scenario())
