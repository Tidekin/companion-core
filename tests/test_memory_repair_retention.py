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


def test_siblings_sharing_a_quote_need_distinct_attributes_after_repair():
    from companion.memory_work import _repair_covers

    quote = "值守室空调设为24度，讲座更正到海盐厅。"
    temp = candidate(quote, key="温度", content="空调设为24度").model_dump()
    venue = candidate(quote, key="讲座场地", content="讲座地点是海盐厅").model_dump()
    renamed = {**venue, "fact_key": "lecture_venue"}
    assert _repair_covers([temp, venue], [renamed, temp])
    assert not _repair_covers([temp, venue], [renamed])
    assert not _repair_covers([temp, venue], [venue, renamed])


def test_reextraction_with_shared_quote_cannot_commit_venue_without_temperature(core):
    from tests.conftest import ingest

    setup(core)
    text = "值守室空调设为24度，讲座更正到海盐厅。"
    ingest(core, 3, text)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    temp = candidate(text, number=3, key="温度", content="空调设为24度")
    venue = candidate(text, number=3, key="场地", content="讲座更正到海盐厅")
    extraction(core, [temp, venue])
    relation = {"supported": True, "update_authorized": False, "claims_replacement": False,
                "same": None, "conflicts": [], "replaces": []}
    core.model.review = lambda _: encode({"relations": [
        relation, {**relation, "update_authorized": True, "claims_replacement": True}]})
    assert run(core.extract_step()).progressed
    assert not run(core.extract_step()).progressed
    extraction(core, [venue.model_copy(update={"update": "replaces_previous"})])
    core.clock.advance(61)
    assert not run(core.extract_step()).progressed
    batch = checkpoint_state(core.store)["batch"]
    assert batch["review_feedback"]["reason"] == "repair_incomplete"
    assert len(batch["repair_candidates"]) == 2
