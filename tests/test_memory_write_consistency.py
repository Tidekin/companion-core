"""Source-grounded repair over the existing bounded extraction/review checkpoint."""

import json

import pytest

from companion.contracts import Candidate, CoreError
from companion.core import Core
from companion.memory_recovery import PREFIX, failures, retry
from companion.memory_work import checkpoint_state
from companion.store import encode
from tests.conftest import ingest, run
from tests.support.confirmed_memory import remember
from tests.support.memory_write_consistency import (
    CONTRADICTION,
    CORRECTION,
    OLD,
    candidate,
    extraction,
    failed_semantic_batch,
    setup,
)


@pytest.mark.parametrize("correction", [True, False], ids=["explicit", "unexplained"])
def test_source_review_disagreement_reextracts_then_reviews_before_commit(core, correction):
    old = setup(core, correction=correction)
    assert run(core.extract_step()).progressed
    assert not run(core.extract_step()).progressed
    work = checkpoint_state(core.store)["batch"]
    assert work["phase"] == "EXTRACT" and work["repair_required"]
    assert work["review_attempts"] == 1
    assert work["review_feedback"]["issues"] == [{"candidate": "c0",
        "reason": "update_marker_mismatch" if correction else "unsupported_replacement_claim",
        "expected_update": "replaces_previous" if correction else None}]
    assert len(core.store.rows("SELECT * FROM memories")) == 1
    text = CORRECTION if correction else CONTRADICTION
    extraction(core, [candidate(text, update="replaces_previous" if correction else None)])
    core.clock.advance(61)
    assert run(core.extract_step()).progressed
    feedback = json.loads(core.model.calls[-1][0][-1]["content"])["review_feedback"]
    assert feedback["reason"] == "update_disagreement" and feedback["candidates"]
    assert run(core.extract_step()).progressed
    assert checkpoint_state(core.store)["batch"]["phase"] == "COMPLETE"
    rows = core.store.rows("SELECT * FROM memories ORDER BY record_sequence")
    assert len(rows) == 2
    if correction:
        assert rows[0]["valid_to"] and rows[1]["needs_review"] == 0
        assert len(core.memory_context.history(old, core.clock())["revisions"]) == 2
    else:
        assert all(r["valid_to"] is None and r["needs_review"] == 1 for r in rows)
        assert all("有效" not in r["content"] and "取代" not in r["content"] for r in rows)
    assert [phase for _, phase in core.model.calls] == [
        "memory_extract", "memory_review", "memory_extract", "memory_review"]
    assert all(r["state"] == "SETTLED" for r in core.store.rows("SELECT * FROM model_usage"))


def test_failed_disagreement_recovers_through_extraction_after_restart_once(core):
    record = failed_semantic_batch(core)
    archived = json.loads(core.store.one("SELECT value FROM runtime_meta WHERE key=%s",
                         (PREFIX + record["batch_id"],))["value"])
    assert archived["batch"]["candidates"][0].get("update") is None
    assert archived["batch"]["review_feedback"]["issues"]
    assert "review_feedback" not in record["batch"]
    calls = len(core.model.calls)
    assert not run(core.extract_step()).progressed and len(core.model.calls) == calls
    extraction(core, [candidate(CORRECTION, update="replaces_previous")])
    with Core(core.settings, core.model, core.clock) as restarted:
        retry(restarted, record["batch_id"], "owner-repair")
        assert checkpoint_state(restarted.store)["batch"]["phase"] == "EXTRACT"
        assert run(restarted.extract_step()).progressed
        assert run(restarted.extract_step()).progressed
        assert failures(restarted.store)[0]["state"] == "COMPLETE"
        with pytest.raises(CoreError, match="MEMORY_RECOVERY_EXHAUSTED"):
            retry(restarted, record["batch_id"], "second-repair")
    assert len(core.store.rows("SELECT * FROM memories")) == 2


def test_repeated_semantic_disagreement_is_bounded_and_retained(core):
    setup(core)
    for _ in range(8):
        run(core.extract_step())
        core.clock.advance(61)
    assert checkpoint_state(core.store)["batch"]["phase"] == "FAILED"
    assert failures(core.store)[0]["recovery_available"]
    assert len(core.model.calls) <= 2 * core.settings.max_attempts
    before = len(core.model.calls)
    assert not run(core.extract_step()).progressed and len(core.model.calls) == before


def test_unsupported_replacement_prose_is_repaired_even_without_update_marker(core):
    setup(core, correction=False)
    extraction(core, [candidate(CONTRADICTION, content="台布天蓝取代砖红，现在有效的是天蓝。")])
    assert run(core.extract_step()).progressed
    assert not run(core.extract_step()).progressed
    work = checkpoint_state(core.store)["batch"]
    assert work["phase"] == "EXTRACT" and work["repair_required"]
    assert work["review_feedback"]["issues"][0]["reason"] == "unsupported_replacement_claim"
    assert len(core.store.rows("SELECT * FROM memories")) == 1


def test_authorized_correction_cannot_degrade_to_two_current_conflicting_values(core):
    old = setup(core)
    item = candidate(CORRECTION, update="replaces_previous")
    core.model.review = lambda data: encode({"relations": [{"supported": True,
        "update_authorized": True, "claims_replacement": True,
        "same": None, "conflicts": ["m0"], "replaces": []}]})
    with pytest.raises(CoreError, match="MEMORY_REVIEW_INVALID"):
        run(core.prepare_memories([item]))
    audit = core.store.one(
        'SELECT safe_detail_json FROM audit_log WHERE '
        "action='memory.review_invalid' ORDER BY "
        'record_sequence DESC LIMIT 1'
    )
    assert json.loads(audit["safe_detail_json"])["reason"] == "missing_replacement_target"
    assert len(core.store.rows("SELECT * FROM memories")) == 1
    assert core.store.one("SELECT valid_to FROM memories WHERE memory_id=%s", (old,)) == {
        "valid_to": None}


def test_single_same_array_does_not_block_correct_cabinet_in_same_batch(core):
    old = setup(core)
    paper = remember(core, 3, "刚从信箱取了报纸。", subject="user", fact_key="paper")
    core.store.execute("UPDATE events SET memory_processed=1 WHERE event_id='evt_test_3'")
    ingest(core, 4, "刚从信箱取了报纸。")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    values = [candidate(CORRECTION, update="replaces_previous"), Candidate(
        subject="user", kind="fact", fact_key="paper", content="主人刚从信箱取了报纸。",
        evidence_event_id="evt_test_4", evidence_quote="刚从信箱取了报纸。")]
    extraction(core, values)

    def review(data):
        refs = {r["content"]: r["ref"] for r in data["existing"]}
        return encode({"relations": [
            {"supported": True, "update_authorized": True, "claims_replacement": True, "same": None,
             "conflicts": [refs[OLD]], "replaces": [refs[OLD]]},
            {"supported": True, "update_authorized": False, "claims_replacement": False,
             "same": [refs["刚从信箱取了报纸。"]], "conflicts": [], "replaces": []}]})

    core.model.review = review
    assert run(core.extract_step()).progressed
    assert run(core.extract_step()).progressed
    assert core.store.one("SELECT valid_to FROM memories WHERE memory_id=%s", (old,))["valid_to"]
    assert len(core.store.rows("SELECT * FROM memory_evidence WHERE memory_id=%s", (paper,))) == 2
    assert len(core.store.rows("SELECT * FROM memories")) == 3


@pytest.mark.parametrize("same", [[], ["m0", "m1"], [42], ["m999"], [{"ref": "m0"}]])
def test_same_normalization_keeps_strict_reference_and_type_checks(core, same):
    setup(core, correction=False)
    c = candidate(CONTRADICTION)
    core.model.review = lambda data: encode({"relations": [{"supported": True,
        "update_authorized": False,
            "claims_replacement": False, "same": same, "conflicts": [], "replaces": []}]})
    with pytest.raises(CoreError, match="MEMORY_REVIEW_INVALID"):
        run(core.prepare_memories([c]))
    assert len(core.store.rows("SELECT * FROM memories")) == 1
