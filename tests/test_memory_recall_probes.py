"""Fixed, offline query-to-source probes, independent of final answer generation."""

import hashlib
import json
from pathlib import Path

from companion.memory_context import MemoryContext
from companion.store import after, encode
from tests.test_m1 import remember


def test_frozen_entity_time_and_scope_probes(core, record_property):
    path = Path(__file__).parent / "fixtures/memory-recall-probes-v1.json"
    raw = path.read_bytes()
    fixture = json.loads(raw)
    assert len(fixture["queries"]) == 50
    ids = {key: remember(core, i, text, fact_key=fact_key, confirmed=True)
           for i, (key, text, fact_key) in enumerate(fixture["facts"], 1)}
    core.clock.advance(86400)
    ids["delivery_new"] = core.memory.decide(ids["delivery_old"], "correct", core.clock(),
        "项目资料现在放在西门值班室。原南门接待处已停用。")["memory_id"]
    core.store.execute("UPDATE memories SET valid_to=? WHERE memory_id=?",
                       (after(core.clock(), -1), ids["expired"]))
    core.memory.decide(ids["erased"], "forget", core.clock())
    core.store.execute("UPDATE memories SET privacy_scope='public' WHERE memory_id=?",
                       (ids["denied"],))
    expected_total = recalled = extra = negatives = negative_correct = 0
    cases = []
    forbidden = {ids[key] for key in ("delivery_old", "expired", "erased", "denied")}
    for name, query, expected_names in fixture["queries"]:
        result = MemoryContext(core.memory).recall(query, core.clock())
        found = {row["memory_id"] for row in result["rows"]}
        expected = {ids[key] for key in expected_names}
        assert not found & forbidden
        expected_total += len(expected)
        recalled += len(expected & found)
        extra += len(found - expected)
        negatives += not expected
        negative_correct += not expected and not found
        cases.append({"case": name, "expected": expected_names,
                      "returned": [key for key, mid in ids.items() if mid in found]})
    metrics = {"fixture_sha256": hashlib.sha256(raw).hexdigest(), "queries": len(cases),
        "necessary_facts": expected_total, "recalled_necessary_facts": recalled,
        "extra_candidates": extra, "negative_queries": negatives,
        "correct_empty_queries": negative_correct, "forbidden_source_leaks": 0,
        "provider_calls": 0, "cases": cases}
    record_property("memory_recall_probe_metrics", encode(metrics))
    assert not core.store.rows("SELECT * FROM model_usage")
