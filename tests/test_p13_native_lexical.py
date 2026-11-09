"""The offline lexical experiment uses the same native nomination as Memory."""

from companion.contracts import Candidate
from scripts.p13_memory_research import lexical_ids
from tests.conftest import ingest


def test_lexical_research_uses_native_terms_and_restricts_the_eligible_pool(core):
    rows = []
    for number, text in enumerate(("纸伞修复工作在青榆伞坊。", "另一个纸伞修复项目在西街。"), 1):
        event_id = ingest(core, number, text)["event_id"]
        with core.store.transaction(control_write=True):
            key = core.memory.add(Candidate(
                subject="user", kind="fact", fact_key=str(number), content=text,
                evidence_event_id=event_id, evidence_quote=text,
            ), core.clock(), confirmed=True)
        rows.append({"memory_id": key, "content": text})
    assert lexical_ids(core.store, "纸伞修复", rows[:1]) == [rows[0]["memory_id"]]
    assert lexical_ids(core.store, "纸伞修复", []) == []
    assert lexical_ids(core.store, "修复", rows[:1]) == [rows[0]["memory_id"]]
