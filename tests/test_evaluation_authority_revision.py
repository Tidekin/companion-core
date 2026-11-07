"""A stopped evaluation permission revision retains the original financial custody."""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from companion import model_carry, owner_authority
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityChange, AuthorityPreview, AuthorityStop
from companion_delivery_protocol import digest
from tests.model_carry_support import migrated
from tests.support.evaluation_authority_revision import revise, revised_policy, stop
from tests.support.model_carry_lifecycle import current_unknown
from tests.support.model_renewal import renewal
from tests.support.owner_authority import activate


def test_revision_preserves_unknown_journal_records_and_resumes_same_policy(core, tmp_path):
    case = migrated(core, tmp_path)
    rows = core.store.rows("SELECT * FROM model_usage")
    journal = Path(core.settings.paid_model.journal_path)
    prefix = journal.read_bytes()
    request = revise(core, case)
    assert journal.read_bytes().startswith(prefix)
    assert core.store.rows("SELECT * FROM model_usage") == rows
    assert owner_authority.current(core.store, core.settings, core.clock,
                                   purpose="evaluation").authority.as_dict() == case.successor[
                                       "authority"]
    before = journal.read_bytes()
    assert owner_authority.activate(core.store, core.settings, core.clock, request) == (
        case.successor)
    assert journal.read_bytes() == before
    stop(core, case, "revised-policy-second-stop")
    result, _ = activate(core, case.policy, revision=6, operation="revised-policy-resume")
    assert result["authority"]["revision"] == 7
    assert core.store.rows("SELECT * FROM model_usage") == rows
    view = owner_authority.custody(core.store, core.settings)
    assert len(view["model_carries"]) == 1
    assert model_carry.scope_windows(view, core.settings.paid_model.window_id) == [
        "offline-window-1", "successor-window-1"]


@pytest.mark.parametrize("change", ["authorization", "purpose", "paid", "carry", "expiry",
                                    "retention", "capture", "history", "reply", "remove"])
def test_revision_rejects_other_changes_without_writes(core, tmp_path, change):
    case = migrated(core, tmp_path)
    stop(core, case)
    policy = revised_policy(case)
    updates = {
        "authorization": {"authorization_sha256": case.policy.authorization_sha256},
        "purpose": {"purposes": [*case.policy.purposes, "reflection"]},
        "paid": {"paid_contract_sha256": "b" * 64},
        "carry": {"unknown_carry_sha256": "b" * 64},
        "expiry": {"expires_at": core.clock.now + timedelta(days=1)},
        "retention": {"source_retention_days": policy.source_retention_days - 1},
        "capture": {"capture": policy.capture.model_copy(update={"public_key": "b" * 64})},
        "history": {"memory_scope": "owner_private"},
        "reply": {"max_reply_chars": policy.max_reply_chars - 1},
        "remove": {"purposes": ["evaluation"]},
    }
    policy = policy.model_copy(update=updates[change])
    before = (list(core.store.db.iterdump()),
              Path(core.settings.paid_model.journal_path).read_bytes())
    with pytest.raises((CoreError, ValueError)):
        activate(core, policy, revision=4, operation="invalid-evaluation-revision")
    assert (list(core.store.db.iterdump()),
            Path(core.settings.paid_model.journal_path).read_bytes()) == before


@pytest.mark.parametrize("boundary", ["active", "current_unknown", "restore", "blocked",
                                     "erased_current", "missing_revision"])
def test_revision_requires_current_stopped_verified_custody(core, tmp_path, boundary):
    case = migrated(core, tmp_path)
    policy = revised_policy(case)
    if boundary == "current_unknown":
        current_unknown(core)
    if boundary != "active":
        stop(core, case)
    if boundary in {"restore", "blocked"}:
        name = "model_budget_restore_required" if boundary == "restore" else "model_budget_blocked"
        core.store.execute("INSERT INTO runtime_meta VALUES (?, '1')", (name,))
    elif boundary == "erased_current":
        core.store.execute("UPDATE owner_authority_revisions "
                           "SET policy_json='{}',policy_redacted=1 "
                           "WHERE revision IN (3,4)")
    elif boundary == "missing_revision":
        core.store.execute("DELETE FROM owner_authority_revisions WHERE revision=2")
    revision = owner_authority._latest(core.store)["revision"]
    before = (list(core.store.db.iterdump()),
              Path(core.settings.paid_model.journal_path).read_bytes())
    with pytest.raises(CoreError):
        owner_authority.preview(core.store, core.settings, core.clock,
            AuthorityPreview(expected_revision=revision, policy=policy))
    assert (list(core.store.db.iterdump()),
            Path(core.settings.paid_model.journal_path).read_bytes()) == before


@pytest.mark.parametrize("new_unknown", [False, True])
def test_revised_policy_can_carry_new_unknown_and_renew(core, tmp_path, new_unknown):
    case = migrated(core, tmp_path)
    revise(core, case)
    if new_unknown:
        rid = current_unknown(core)
    original = core.store.rows("SELECT * FROM model_usage")
    request, policy, witness = renewal(core, case)
    receipt = model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    if new_unknown:
        proof = model_carry.read(core.store, core.settings, case.successor["authority"],
                                 receipt["carry_sha256"], digest(policy.model_dump(
                                     mode="json")))
        assert proof["request_ids"] == [rid]
        assert proof["successor_policy_sha256"] == receipt["successor_policy_sha256"]
    active, _ = activate(core, policy, revision=case.successor["authority"]["revision"] + 1,
                         operation="revised-renewal-activation")
    assert active["state"] == "ACTIVE"
    assert core.store.rows("SELECT * FROM model_usage") == original
    assert "evaluation" in owner_authority.current(
        core.store, core.settings, core.clock).policy.purposes
    assert len(owner_authority.custody(core.store, core.settings)["model_carries"]) == 2


def test_revised_current_unknown_keeps_its_actual_authority_in_local_receipt(core, tmp_path):
    case = migrated(core, tmp_path)
    revise(core, case)
    rid = current_unknown(core)
    stop(core, case, "revised-local-stop")
    proof = model_carry.read(core.store, core.settings, case.successor["authority"],
                             case.receipt["carry_sha256"],
                             digest(case.policy.model_dump(mode="json")))
    assert proof["version"] == 2 and proof["request_ids"] == [rid]
    assert proof["authority"] == case.successor["authority"]
    assert proof["successor_policy_sha256"] == case.successor["authority"]["policy_sha256"]
    activate(core, case.policy, revision=6, operation="revised-local-resume")
    assert model_carry.read(core.store, core.settings, case.successor["authority"],
                            case.receipt["carry_sha256"], proof["successor_policy_sha256"]) == proof


def test_missing_policy_body_preserves_financial_replay_but_cannot_activate(core, tmp_path):
    case = migrated(core, tmp_path)
    revise(core, case)
    request, policy, witness = renewal(core, case)
    model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    active, _ = activate(core, policy, revision=6, operation="revised-renewal-activation")
    before = core.store.rows("SELECT * FROM model_usage")
    # Same projection used by source erasure: no policy body can be reconstructed.
    core.store.execute("UPDATE owner_authority_revisions SET policy_json='{}',policy_redacted=1 "
                       "WHERE revision BETWEEN 3 AND 6")
    assert len(owner_authority.custody(core.store, core.settings)["model_carries"]) == 2
    assert core.store.rows("SELECT * FROM model_usage") == before
    assert all(row["policy_json"] == "{}" for row in core.store.rows(
        "SELECT policy_json FROM owner_authority_revisions WHERE revision BETWEEN 3 AND 6"))
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="erased-policy-stop", authority=active["authority"]))
    core.store.execute("UPDATE owner_authority_revisions SET policy_json='{}',policy_redacted=1 "
                       "WHERE revision IN (7,8)")
    with pytest.raises(CoreError):
        activate(core, policy, revision=8, operation="erased-policy-resume")


def test_revision_preview_cannot_cross_another_authority_change(core, tmp_path):
    case = migrated(core, tmp_path)
    stop(core, case)
    policy = revised_policy(case)
    preview = owner_authority.preview(core.store, core.settings, core.clock,
        AuthorityPreview(expected_revision=4, policy=policy))
    activate(core, case.policy, revision=4, operation="original-policy-resume")
    with pytest.raises(CoreError, match="REVISION_CHANGED"):
        owner_authority.activate(core.store, core.settings, core.clock, AuthorityChange(
            operation_id="stale-evaluation-revision", expected_revision=4, policy=policy,
            preview_sha256=preview["preview_sha256"]))
    assert json.loads(owner_authority._latest(core.store)["policy_json"])["purposes"] == (
        case.policy.purposes)
