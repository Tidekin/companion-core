"""Long-history async retrieval must deliver extracted evidence within one Work."""
import itertools

import pytest

from companion import retrieval_work
from companion.store import encode
from tests.conftest import run
from tests.support.enriched_recall import deterministic_vectors, extracted_history


@pytest.mark.parametrize("sql_budget", [1000, 20000])
def test_async_extracted_history_preserves_evidence_at_the_original_budget(
    core, monkeypatch, record_property, sql_budget,
):
    target = extracted_history(core)
    deterministic_vectors(monkeypatch, core.memory)
    # Real native provenance/transaction reads with deterministic 20ms transport
    # cost; the larger allowance is an explicit diagnostic ablation, never config.
    ticks = itertools.count(0, .02)
    monkeypatch.setattr(retrieval_work, "monotonic", lambda: next(ticks))
    monkeypatch.setitem(retrieval_work.LIMITS, "sql_milliseconds", sql_budget)
    trace = {}
    result = run(core.memory_context.recall_async(
        "archive_box_KX71", core.clock(), clock=core.clock, trace=trace))
    record_property("enriched_retrieval", encode({"budget": sql_budget,
        "state": result["state"], "facts": len(result["facts"]), "trace": trace}))
    assert target in {row["memory_id"] for row in result["facts"]}, trace
    assert all(0 <= value <= trace["work"]["limits"][name]
               for name, value in trace["work"]["counts"].items())
    assert any("绿柜二层" in item["content"] for item in result["facts"])
