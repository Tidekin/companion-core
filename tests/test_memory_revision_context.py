"""A current hit cannot conceal supported older versions from temporal questions."""
import json

from companion.contracts import Candidate
from tests.conftest import ingest
from tests.support.confirmed_memory import remember
from tests.support.dialogue_context import current_job


def test_final_context_keeps_complete_revision_chain_and_withdraws_erased_source(core):
    first = remember(core, 1, "晨渡分享会先定荻声厅。", subject="project", fact_key="venue")
    previous = first
    for number, content in ((2, "晨渡分享会改到枫灯厅，荻声厅取消。"),
                            (3, "晨渡分享会最终改到榕雪厅，枫灯厅取消。")):
        core.clock.advance(3600)
        ingest(core, number, content)
        candidate = Candidate(subject="project", kind="fact", fact_key="venue", content=content,
            evidence_event_id=f"evt_test_{number}", evidence_quote=content,
            speaker="owner", update="replaces_previous")
        previous = core.memory.add(candidate, core.clock(), resolution={
            "same": None, "conflicts": [previous], "replaces": [previous]})
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    row = {**core.store.one("SELECT * FROM memories WHERE memory_id=%s", (previous,)),
           "evidence_ids": ["evt_test_3"]}
    job = current_job(core, 4, "晨渡分享会最初定哪里，房间依次怎么变？")
    messages = core.context(job, memories=[row])
    chains = []
    for message in messages:
        if isinstance(message.get("content"), str) and '"memory_history"' in message["content"]:
            content = message["content"]
            chains.append(json.loads(content[content.index("{"):])["memory_history"])
    assert len(chains) == 1
    assert [r["content"] for r in chains[0]["revisions"]] == [
        "晨渡分享会最终改到榕雪厅，枫灯厅取消。", "晨渡分享会改到枫灯厅，荻声厅取消。",
        "晨渡分享会先定荻声厅。"]
    assert not chains[0]["truncated"]
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    chain = core.memory_context.history(previous, core.clock())
    assert first not in {r["memory_id"] for r in chain["revisions"]}
