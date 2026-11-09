"""Actual EXTRACT/REVIEW commits, including salience and native source proofs."""
import numpy as np

from companion.store import encode
from tests.conftest import ingest, run


def extracted_history(core, count=48):
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 8
    core.settings.extraction_idle_seconds = 0
    for start in range(0, count, 8):
        candidates = []
        for index in range(start, min(start+8, count)):
            number = index+1
            key = "archive_box_KX71" if index == 0 else f"shelf_note_{number}"
            content = ("芦虹资料铜盒放在绿柜二层。" if index == 0
                       else f"第{number}次展务整理使用编号{number}的纸袋。")
            ingest(core, number, content)
            candidates.append({"subject": "user", "kind": "fact", "fact_key": key,
                               "content": content, "evidence_event_id": f"evt_test_{number}",
                               "evidence_quote": content, "salience": 8})
        core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
        core.model.extraction = encode({"memories": candidates})
        assert run(core.extract_once())
        core.clock.advance(3600)
    assert core.store.one("SELECT COUNT(*) n FROM memories")["n"] == count
    assert core.store.one("SELECT COUNT(*) n FROM runtime_meta WHERE key LIKE "
                          "'memory_importance:%%'")["n"] == count
    return core.store.one("SELECT memory_id FROM memories WHERE fact_key='archive_box_KX71'")[
        "memory_id"]


def deterministic_vectors(monkeypatch, memory):
    monkeypatch.setattr(memory.index, "_infer", lambda query, texts: (
        tuple(np.ones(512)/np.sqrt(512) for _ in texts), np.ones(512)/np.sqrt(512)))
