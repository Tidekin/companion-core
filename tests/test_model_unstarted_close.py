"""An UNKNOWN the journal proves never started I/O can be closed offline under STOP."""

import pytest

from companion import budget, model_carry, owner_authority
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from tests.model_carry_support import MESSAGES, migrated
from tests.owner_model_fixtures import authorized_job
from tests.support.model_carry_lifecycle import current_unknown
from tests.support.owner_authority import activate


def unstarted_unknown(core):
    # Production 2026-10-29: a refusal after admission but before `io_started`
    # settled the attempt UNKNOWN with io_started_at null and no local closure.
    job = authorized_job(core, number=2)
    request_id = budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())
    budget.settle(core.store, core.settings, request_id)
    assert core.store.one('SELECT state FROM model_usage WHERE request_id=%s',
                          (request_id,))["state"] == "UNKNOWN"
    return request_id


def stop(core, case):
    return owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="unstarted-close-stop", authority=case.successor["authority"]))


def test_closure_frees_the_slot_and_lets_the_same_policy_resume(core, tmp_path):
    case = migrated(core, tmp_path)
    request_id = unstarted_unknown(core)
    with pytest.raises(CoreError, match="MODEL_PROVIDER_CONCURRENCY_EXHAUSTED"):
        budget.reserve(core.store, core.settings, authorized_job(core, number=3), "chat",
                       MESSAGES, core.clock())
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_STOP_REQUIRED"):
        model_carry.close_unstarted(core.store, core.settings, request_id, core.clock(),
                                    commit=True)
    stop(core, case)
    with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_NOT_QUIESCENT"):
        activate(core, case.policy, revision=4, operation="resume-before-close")
    before = core.store.rows("SELECT * FROM model_usage")
    preview = model_carry.close_unstarted(core.store, core.settings, request_id, core.clock(),
                                          commit=False)
    assert not preview["committed"] and preview["record"]["io_started_at"] is None
    with core.store.transaction():
        assert request_id not in owner_authority.custody(
            core.store, core.settings, settlement=True)["model_closures"]
    result = model_carry.close_unstarted(core.store, core.settings, request_id, core.clock(),
                                         commit=True)
    assert result["committed"] and result["retained_micro_usd"] > 0
    with pytest.raises(CoreError, match="MODEL_UNSTARTED_CLOSE_NOT_ELIGIBLE"):
        model_carry.close_unstarted(core.store, core.settings, request_id, core.clock(),
                                    commit=True)
    # The charge is untouched: still UNKNOWN at the same amount in every total.
    assert core.store.rows("SELECT * FROM model_usage") == before
    activate(core, case.policy, revision=4, operation="resume-after-close")
    assert owner_authority._latest(core.store)["state"] == "ACTIVE"
    with core.store.transaction():
        view = owner_authority.custody(core.store, core.settings, settlement=True)
    # Admission's execution-slot count skips closed requests.
    assert request_id in view["model_closures"]


def test_a_started_unknown_is_never_eligible(core, tmp_path, monkeypatch):
    case = migrated(core, tmp_path)
    monkeypatch.setattr(model_carry, "close_owned", lambda *args: None)
    request_id = current_unknown(core)
    stop(core, case)
    with pytest.raises(CoreError, match="MODEL_UNSTARTED_CLOSE_NOT_ELIGIBLE"):
        model_carry.close_unstarted(core.store, core.settings, request_id, core.clock(),
                                    commit=True)
