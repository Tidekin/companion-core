"""Repair cannot turn retained facts into silently discarded or replayed work."""

import pytest

from companion.contracts import CoreError
from companion.memory_recovery import failures, retry
from companion.memory_work import checkpoint_state
from companion.store import encode
from tests.conftest import run
from tests.test_memory_write_consistency import (
    CORRECTION,
    candidate,
    extraction,
    failed_semantic_batch,
    setup,
)


def test_reextraction_cannot_silently_drop_the_disputed_fact(core):
    setup(core)
    assert run(core.extract_step()).progressed
    assert not run(core.extract_step()).progressed
    extraction(core, [])
    core.clock.advance(61)
    assert not run(core.extract_step()).progressed
    work = checkpoint_state(core.store)["batch"]
    assert work["error"] == "MEMORY_REVIEW_DISAGREEMENT" and work["repair_required"]
    assert work["review_feedback"]["reason"] == "repair_incomplete"
    assert work["repair_candidates"][0]["evidence_quote"] == CORRECTION
    assert len(core.store.rows("SELECT * FROM memories")) == 1
    core.clock.advance(61)
    assert run(core.extract_step()).progressed
    assert checkpoint_state(core.store)["batch"]["phase"] == "FAILED"
    assert failures(core.store)[0]["recovery_available"]


def test_second_review_rejection_retains_the_fact_for_bounded_repair(core):
    setup(core)
    assert run(core.extract_step()).progressed
    assert not run(core.extract_step()).progressed
    extraction(core, [candidate(CORRECTION, update="replaces_previous")])
    core.clock.advance(61)
    assert run(core.extract_step()).progressed
    core.model.review = lambda data: encode({"relations": [{"supported": False,
        "update_authorized": True, "claims_replacement": True,
        "same": None, "conflicts": [], "replaces": []}]})
    assert not run(core.extract_step()).progressed
    work = checkpoint_state(core.store)["batch"]
    assert work["phase"] == "EXTRACT"
    assert work["review_feedback"]["issues"][0]["reason"] == "repair_candidate_rejected"
    assert len(core.store.rows("SELECT * FROM memories")) == 1


def test_guarded_failure_erasure_clears_feedback_but_preserves_accounting(core):
    record = failed_semantic_batch(core)
    with core.store.transaction():
        core.ledger.expire_events(core.store, record["source_event_ids"], core.clock())
    assert failures(core.store) == [] and checkpoint_state(core.store) is None
    assert all(r["state"] == "SETTLED" for r in core.store.rows("SELECT * FROM model_usage"))


def test_guarded_failure_cannot_replay_unknown_extraction(core):
    record = failed_semantic_batch(core)
    core.store.execute("UPDATE model_usage SET state='UNKNOWN' WHERE request_id=?",
                       (record["batch"]["extraction_request_id"],))
    calls = len(core.model.calls)
    with pytest.raises(CoreError, match="MEMORY_RECOVERY_UNSETTLED"):
        retry(core, record["batch_id"], "owner-request")
    assert len(core.model.calls) == calls and failures(core.store)[0]["state"] == "FAILED"
