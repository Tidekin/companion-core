"""Frozen public synthetic recall set. Candidate quality is distinct from reply quality."""

import hashlib
import json

from companion.memory_context import MemoryContext
from companion.store import after, encode
from tests.support.confirmed_memory import remember

FACTS = (
    ("tea", "我平时最爱喝的饮料是茉莉绿茶。", "drink"),
    ("exercise", "我每逢休息日会去公园慢跑。", "exercise"),
    ("work_lin", "青禾项目的小林负责周三的交接。", "work_lin"),
    ("club_lin", "读书会的小林负责周六的科幻分享。", "club_lin"),
    ("handoff_old", "青禾项目的资料原来放在东门。", "handoff"),
    ("color", "我最喜欢的颜色是蓝色。", "color"),
    ("conflict_a", "秋游计划选海边。", "outing"),
    ("conflict_b", "秋游计划选山里。", "outing"),
    ("expired", "上周临时取件点在北门。", "temporary"),
)
QUERIES = (
    ("cross_day_indirect", "我平时最爱喝什么？", ("tea",)),
    ("indirect_exercise", "休息时我通常会怎样锻炼？", ("exercise",)),
    ("same_name_work", "青禾项目的小林负责什么？", ("work_lin",)),
    ("same_name_club", "读书会的小林负责什么？", ("club_lin",)),
    ("same_name_ambiguous", "小林负责什么？", ("work_lin", "club_lin")),
    ("correction", "青禾项目的资料放在哪里？", ("handoff_new",)),
    ("attribute_ambiguity", "我最喜欢什么颜色？", ("color",)),
    ("conflict", "秋游计划", ("conflict_a", "conflict_b")),
    ("no_record", "我的宠物叫什么名字？", ()),
    ("time_expired", "上周临时取件点", ()),
)


def test_frozen_recall_quality_and_provenance(core, record_testsuite_property):
    core.settings.memory_enabled = True
    ids = {name: remember(core, n, content, fact_key=key, confirmed=True)
           for n, (name, content, key) in enumerate(FACTS, 1)}
    core.clock.advance(86400)
    ids["handoff_new"] = core.memory.decide(ids["handoff_old"], "correct", core.clock(),
                                            "青禾项目的资料现在放在西门。")["memory_id"]
    core.store.execute('UPDATE memories SET valid_to=%s WHERE memory_id=%s',
                       (after(core.clock(), -1), ids["expired"]))
    view, cases = MemoryContext(core.memory), []
    expected_total = recalled = false_positive = evidence_total = complete_evidence = 0
    for name, query, expected_names in QUERIES:
        result = view.recall(query, core.clock())
        items = [*result["facts"], *result["conflicts"]]
        found = {item["memory_id"] for item in items}
        expected = {ids[key] for key in expected_names}
        expected_total += len(expected)
        recalled += len(found & expected)
        false_positive += len(found - expected)
        for item in items:
            evidence_total += 1
            complete_evidence += bool(item["evidence"] and all(
                proof["event_id"] and proof["source"] and proof["occurred_at"]
                and proof["ingested_at"] for proof in item["evidence"]
            ))
        cases.append({"case": name, "expected": list(expected_names), "state": result["state"],
                      "returned": [key for key, value in ids.items() if value in found]})
        assert ids["handoff_old"] not in found and ids["expired"] not in found
    metrics = {
        "dataset_sha256": hashlib.sha256(encode([FACTS, QUERIES]).encode()).hexdigest(),
        "queries": len(QUERIES), "necessary_facts": expected_total,
        "recalled_necessary_facts": recalled, "false_positive_candidates": false_positive,
        "returned_candidates": evidence_total, "complete_source_candidates": complete_evidence,
        "no_record_correct": sum(case["state"] == "NO_RECORD" for case in cases
                                 if not case["expected"]),
        "no_record_cases": 2, "superseded_or_expired_leaks": 0,
        "provider_calls": 0, "real_usage": None, "cases": cases,
    }
    record_testsuite_property("next_memory_metrics", json.dumps(metrics, ensure_ascii=False))
    assert complete_evidence == evidence_total
    assert recalled > 0  # Report measured recall honestly; do not tune the frozen set to pass.
