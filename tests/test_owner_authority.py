"""Native journal revision/STOP custody; all approvals and transport are synthetic."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from companion import owner_authority as authority
from companion import paid_budget
from companion.api import create_app
from companion.contracts import CoreError
from companion.owner_authority_protocol import (
    AuthorityPreview,
    AuthorityStop,
    OwnerGrantPolicy,
    OwnerRuntimeConfig,
)
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare


def test_default_off_and_no_implicit_grant(core, tmp_path):
    policy = prepare(core, tmp_path, enabled=False)
    assert core.store.rows("SELECT * FROM owner_authority_revisions") == []
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_DISABLED"):
        activate(core, policy)
    assert not Path(core.settings.paid_model.journal_path).exists()


def test_revision_idempotency_and_current_policy(core, tmp_path):
    policy = prepare(core, tmp_path)
    result, request = activate(core, policy)
    assert authority.activate(core.store, core.settings, core.clock, request) == result
    view = authority.current(core.store, core.settings, core.clock, purpose="evaluation")
    assert view.authority.as_dict() == result["authority"]
    assert view.policy == policy
    assert len(core.store.rows("SELECT * FROM owner_authority_revisions")) == 1
    request = request.model_copy(update={"preview_sha256": "f" * 64})
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_OPERATION_CONFLICT"):
        authority.activate(core.store, core.settings, core.clock, request)
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_REVISION_CHANGED"):
        activate(core, policy, operation="operation-activate-2")


def test_stop_survives_removed_settings_and_does_not_claim_quiescence(core, tmp_path):
    policy = prepare(core, tmp_path)
    result, _ = activate(core, policy)
    core.settings.owner_runtime = OwnerRuntimeConfig()
    core.settings.paid_model = None
    request = AuthorityStop(operation_id="operation-stop-1", authority=result["authority"])
    stopped = authority.stop(core.store, core.settings, core.clock, request)
    assert stopped["state"] == "STOPPED"
    assert stopped["admission_fenced"] and not stopped["quiescence_proven"]
    assert authority.stop(core.store, core.settings, core.clock, request) == stopped
    assert authority.status(core.store, core.settings, core.clock)["state"] == "OFF"


def test_journal_fsync_precedes_database_commit(core, tmp_path, monkeypatch):
    policy = prepare(core, tmp_path)
    # Establish the original journal header so the injected failure targets the revision.
    with core.store.transaction():
        paid_budget.journal(core.store, core.settings.paid_model)
    def fail_fsync(_):
        raise OSError("synthetic fsync failure")
    with monkeypatch.context() as patch:
        patch.setattr(paid_budget.os, "fsync", fail_fsync)
        with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
            activate(core, policy)
    assert core.store.rows("SELECT * FROM owner_authority_revisions") == []
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        activate(core, policy)


def test_restore_generation_cannot_restore_authority(core, tmp_path):
    policy = prepare(core, tmp_path)
    activate(core, policy)
    with core.store.transaction():
        authority.restore(core.store)
    assert authority.status(core.store, core.settings, core.clock)["state"] == "OFF"
    # Even changing the local mode pointer cannot bypass the restore generation.
    core.store.execute("UPDATE runtime_meta SET value='ENABLED' WHERE key=?", (authority.MODE,))
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_REQUIRED"):
        authority.current(core.store, core.settings, core.clock)


def test_stop_record_and_database_projection_cannot_diverge(core, tmp_path):
    policy = prepare(core, tmp_path)
    result, _ = activate(core, policy)
    authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="operation-stop-1", authority=result["authority"],
    ))
    core.store.execute("DELETE FROM owner_authority_revisions WHERE state='STOPPED'")
    with core.store.transaction(), pytest.raises(CoreError,
                                                match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        paid_budget.journal(core.store, core.settings.paid_model, allow_settlement=True)


def projection_history(core, tmp_path):
    policy = prepare(core, tmp_path)
    current, _ = activate(core, policy)
    for index in range(3):
        stopped = authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id=f"projection-stop-{index}", authority=current["authority"],
        ))
        current, _ = activate(core, policy, revision=stopped["authority"]["revision"],
                              operation=f"projection-activate-{index}")
    path = Path(core.settings.paid_model.journal_path)
    records = []
    for line in path.read_text("utf-8").splitlines():
        row = json.loads(line)
        if row["kind"] == authority.RECORD_KIND:
            authority.fold(records, row, row.pop("sha256"))
    journal_id = core.store.one("SELECT value FROM runtime_meta "
                                "WHERE key='model_budget_journal_id'")["value"]
    return path, journal_id, records


def test_projection_resolves_shared_paths_and_hashes_identical_policies_once_per_call(
    core, tmp_path, monkeypatch,
):
    path, journal_id, records = projection_history(core, tmp_path)
    resolutions, policies = [], []
    resolve, sha = Path.resolve, authority._sha

    def resolved(value, *args, **kwargs):
        resolutions.append(str(value))
        return resolve(value, *args, **kwargs)

    def hashed(value):
        policies.append(value)
        return sha(value)

    monkeypatch.setattr(Path, "resolve", resolved)
    monkeypatch.setattr(authority, "_sha", hashed)
    for _ in range(2):
        with core.store.transaction():
            authority.validate_projection(core.store, records, journal_id, path)
    assert len(records) == 7
    # Each call checks the current path again at entry and exit, with no cross-call cache.
    assert resolutions == [str(path)] * 4
    assert len(policies) == 2


@pytest.mark.parametrize("matching", [True, False])
def test_projection_preserves_resolved_path_alias_and_mismatch(core, tmp_path, matching):
    path, journal_id, records = projection_history(core, tmp_path)
    directory = tmp_path / "alias"
    directory.mkdir()
    saved_path = directory / ".." / (path.name if matching else "another-journal.jsonl")
    core.store.execute("UPDATE owner_authority_revisions SET journal_path=?", (str(saved_path),))
    with core.store.transaction():
        if matching:
            authority.validate_projection(core.store, records, journal_id, path)
        else:
            with pytest.raises(ValueError, match="authority projection mismatch"):
                authority.validate_projection(core.store, records, journal_id, path)


def test_projection_rechecks_path_alias_after_policy_validation(core, tmp_path, monkeypatch):
    path, journal_id, records = projection_history(core, tmp_path)
    resolve, sha = Path.resolve, authority._sha
    changed = False

    def resolved(value, *args, **kwargs):
        return resolve(tmp_path / "changed-journal.jsonl" if changed else value, *args, **kwargs)

    def hashed(value):
        nonlocal changed
        result = sha(value)
        changed = True
        return result

    monkeypatch.setattr(Path, "resolve", resolved)
    monkeypatch.setattr(authority, "_sha", hashed)
    with core.store.transaction(), pytest.raises(ValueError, match="authority projection mismatch"):
        authority.validate_projection(core.store, records, journal_id, path)


@pytest.mark.parametrize("change", ["policy", "redacted_body", "erased"])
def test_projection_checks_changed_policy_and_each_redaction(core, tmp_path, change):
    path, journal_id, records = projection_history(core, tmp_path)
    with core.store.transaction():
        authority.validate_projection(core.store, records, journal_id, path)
    if change == "policy":
        value = json.loads(core.store.one("SELECT policy_json FROM owner_authority_revisions "
                                         "WHERE revision=2")["policy_json"])
        value["authorization_sha256"] = "e" * 64
        core.store.execute("UPDATE owner_authority_revisions SET policy_json=? WHERE revision=2",
                           (json.dumps(value),))
    else:
        core.store.execute("UPDATE owner_authority_revisions SET policy_redacted=1 "
                           "WHERE revision=2")
        if change == "erased":
            core.store.execute("UPDATE owner_authority_revisions SET policy_json='{}' "
                               "WHERE revision=2")
    with core.store.transaction():
        if change == "erased":
            authority.validate_projection(core.store, records, journal_id, path)
        else:
            with pytest.raises(ValueError,
                               match="authority policy mismatch|invalid erased authority"):
                authority.validate_projection(core.store, records, journal_id, path)


def test_control_api_rejects_bridge_credential_and_activates_exact_preview(core, tmp_path):
    policy = prepare(core, tmp_path)
    core.settings.control_token = SecretStr("synthetic-owner-control-token")
    app = create_app(core.settings)
    app.state.core = core
    client = TestClient(app)
    request = AuthorityPreview(expected_revision=0, policy=policy).model_dump(mode="json")
    bridge = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
    owner = {"Authorization": "Bearer synthetic-owner-control-token"}
    prefix = "/internal/v1/owner-authority"
    assert client.post(prefix + "/preview", json=request, headers=bridge).status_code == 401
    response = client.post(prefix + "/preview", json=request, headers=owner)
    assert response.status_code == 200, response.text
    request.update(operation_id="operation-api-activate",
                   preview_sha256=response.json()["preview_sha256"])
    response = client.post(prefix + "/activate", json=request, headers=owner)
    assert response.status_code == 200, response.text
    assert client.post(prefix + "/activate", json=request, headers=owner).json() == response.json()
    records = [json.loads(line) for line in Path(core.settings.paid_model.journal_path).read_text(
        "utf-8").splitlines()]
    assert records[-1]["kind"] == authority.RECORD_KIND
    assert "policy" not in records[-1]  # Journal pins policy by hash, without source bodies.


def test_native_encrypted_restore_never_reactivates_grant(core, tmp_path):
    from cryptography.fernet import Fernet

    from companion.backup import backup, restore
    from companion.runtime import Runtime

    policy = prepare(core, tmp_path)
    activate(core, policy)
    path, restored, key = tmp_path / "snapshot.enc", tmp_path / "restored.db", Fernet.generate_key()
    backup(core.settings.database, path, key)
    restore(path, restored, key, core.settings.erasure_ledger)
    settings = core.settings.model_copy(update={"database": str(restored)})
    with Runtime(settings, core.clock) as runtime:
        assert authority.status(runtime.store, settings, core.clock)["state"] == "OFF"
        assert runtime.store.one("SELECT value FROM runtime_meta WHERE key=?",
                                 (authority.GENERATION,))["value"] != "initial"


def test_source_erasure_scrubs_policy_but_preserves_journal_evidence(core, tmp_path):
    from companion.life_protocol import LifeShadowApproval
    from companion.store import after
    from tests.conftest import ingest

    ingest(core)
    policy = prepare(core, tmp_path)
    core.store.execute("UPDATE jobs SET state='CANCELLED'")
    event = core.store.one("SELECT event_id,conversation_id FROM events")
    scope = LifeShadowApproval(
        scope="owner_private_life_shadow_v1", authorization_sha256="b" * 64,
        conversation_id=event["conversation_id"], source_event_ids=[event["event_id"]],
        purposes=["PRIVATE_DIARY"], expires_at=after(core.clock(), 3600), max_items=2,
    )
    policy = policy.model_copy(update={"purposes": ["life"], "life_shadow": scope})
    activated, _ = activate(core, policy)
    with core.store.transaction():
        core.ledger.expire_events(core.store, [event["event_id"]], core.clock())
    row = core.store.one("SELECT * FROM owner_authority_revisions")
    assert row["policy_json"] == "{}" and row["policy_redacted"] == 1
    assert row["record_sha256"] == activated["record_sha256"]
    assert authority.status(core.store, core.settings, core.clock)["state"] == "OFF"
    with core.store.transaction():
        paid_budget.journal(core.store, core.settings.paid_model, allow_settlement=True)


@pytest.mark.parametrize("state", ["OPEN", "PREPARED"])
def test_old_live_window_is_not_converted_to_grant(core, tmp_path, state):
    from companion.store import encode

    policy = prepare(core, tmp_path)
    core.store.insert("runtime_meta", key="live_text_window", value=encode({
        "plan_sha256": "a" * 64, "window_id": "win_" + "b" * 32,
    }))
    core.store.insert("runtime_meta", key="live_text_window:" + "a" * 64,
                      value=encode({"state": state}))
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_LEGACY_STOP_REQUIRED"):
        activate(core, policy)
    assert core.store.rows("SELECT * FROM owner_authority_revisions") == []


def test_stopped_policy_is_evidence_only(core, tmp_path):
    policy = prepare(core, tmp_path)
    result, _ = activate(core, policy)
    authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="operation-stop-evidence", authority=result["authority"],
    ))
    core.settings.paid_model = None
    assert authority.evidence(core.store, core.settings, result["authority"]).policy == policy
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_DISABLED"):
        authority.current(core.store, core.settings, core.clock, authority=result["authority"])


def test_reserved_rem_purpose_cannot_be_activated(core, tmp_path):
    policy = prepare(core, tmp_path)
    policy = OwnerGrantPolicy.model_validate({**policy.model_dump(), "purposes": ["rem"],
                                              "capability_policies": {"rem": "a" * 64}})
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_CAPABILITY_CHANGED"):
        activate(core, policy)
    assert core.store.rows("SELECT * FROM owner_authority_revisions") == []


def test_paid_policy_drift_is_fenced_before_first_model_admission(core, tmp_path):
    from datetime import timedelta

    policy = prepare(core, tmp_path)
    result, _ = activate(core, policy)
    assert core.store.rows("SELECT * FROM model_admissions") == []
    paid = core.settings.paid_model
    core.settings.paid_model = paid.model_copy(update={
        "expires_at": paid.expires_at + timedelta(seconds=30),
    })
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_PAID_POLICY_CHANGED"):
        authority.current(core.store, core.settings, core.clock, purpose="evaluation")
    # Original proof and emergency STOP remain usable with drifted settings.
    assert authority.evidence(core.store, core.settings, result["authority"]).policy == policy
    authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="operation-stop-drift", authority=result["authority"]))


def test_job_authority_requires_native_unexpired_lease(core, tmp_path):
    from companion.store import after
    from tests.conftest import ingest

    source = ingest(core)
    core.store.execute("UPDATE jobs SET state='CANCELLED'")
    policy = prepare(core, tmp_path)
    activate(core, policy)
    job_id = core.new_job(source["event_id"], "evaluation", "native-evaluation-test", core.clock())
    job = core.store.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
    with core.store.transaction():
        authority.bind_job(core.store, core.settings, core.clock, job["job_id"],
                           purpose="evaluation")
    with pytest.raises(CoreError, match="JOB_LEASE_LOST"):
        authority.check_job(core.store, core.settings, core.clock, job,
                            purpose="evaluation", executing=True)
    core.store.execute("UPDATE jobs SET state='RUNNING',lease_token='lease-owner-test',"
                       "lease_until=? WHERE job_id=?", (after(core.clock(), 30), job["job_id"]))
    job = core.store.one("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],))
    authority.check_job(core.store, core.settings, core.clock, job,
                        purpose="evaluation", executing=True)
    core.store.execute("UPDATE jobs SET lease_until=? WHERE job_id=?",
                       (core.clock(), job["job_id"]))
    with pytest.raises(CoreError, match="JOB_LEASE_LOST"):
        authority.check_job(core.store, core.settings, core.clock, job,
                            purpose="evaluation", executing=True)


def test_revision_cannot_reauthorize_a_bound_job(core, tmp_path):
    from tests.conftest import ingest

    source = ingest(core)
    core.store.execute("UPDATE jobs SET state='CANCELLED'")
    policy = prepare(core, tmp_path)
    activate(core, policy)
    job_id = core.new_job(source["event_id"], "evaluation", "native-evaluation-test", core.clock())
    job = core.store.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
    with core.store.transaction():
        authority.bind_job(core.store, core.settings, core.clock, job["job_id"],
                           purpose="evaluation")

    core.store.execute("UPDATE jobs SET state='CANCELLED'")
    activate(core, policy, revision=1, operation="operation-revision-2")
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_REVISION_CHANGED"):
        authority.check_job(core.store, core.settings, core.clock, job, purpose="evaluation")
    core.store.execute("UPDATE jobs SET state='QUEUED'")
    with core.store.transaction(), pytest.raises(CoreError,
                                                match="OWNER_AUTHORITY_REVISION_CHANGED"):
        authority.bind_job(core.store, core.settings, core.clock, job["job_id"],
                           purpose="evaluation")


def test_pending_external_erasure_fences_current_use_without_repairing_original_evidence(
    core, tmp_path, monkeypatch,
):
    from pathlib import Path

    from tests.conftest import ingest

    source = ingest(core)
    core.store.execute("UPDATE jobs SET state='CANCELLED'")
    policy = prepare(core, tmp_path)
    activated, _ = activate(core, policy)
    usage = core.store.rows("SELECT * FROM model_usage")

    def interrupted(*args):
        raise RuntimeError("synthetic erasure apply crash")

    with monkeypatch.context() as patch:
        patch.setattr(core.ledger, "apply", interrupted)
        with pytest.raises(RuntimeError, match="synthetic erasure apply crash"):
            with core.store.transaction():
                core.ledger.expire_events(core.store, [source["event_id"]], core.clock())
    original = Path(core.settings.erasure_ledger).read_bytes()
    with pytest.raises(CoreError, match="ERASURE_REPLAY_REQUIRED"):
        authority.current(core.store, core.settings, core.clock, purpose="evaluation")
    assert Path(core.settings.erasure_ledger).read_bytes() == original
    assert core.store.one("SELECT redacted FROM events")["redacted"] == 0
    assert authority.evidence(core.store, core.settings, activated["authority"]).policy == policy
    assert core.store.rows("SELECT * FROM model_usage") == usage
    stopped = authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="stop-with-pending-erasure", authority=activated["authority"]))
    assert stopped["state"] == "STOPPED"
    core.ledger.replay(core.store)
    assert core.store.one("SELECT redacted FROM events")["redacted"] == 1
    assert Path(core.settings.erasure_ledger).read_bytes() == original
