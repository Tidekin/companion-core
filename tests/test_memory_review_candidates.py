"""Review must see semantic twins without changing ordinary answer ranking."""

from types import SimpleNamespace

from companion.contracts import Candidate
from companion.memory import Memory
from tests.conftest import run
from tests.support.confirmed_memory import remember


def candidate(**values):
    return Candidate(subject="user", kind="preference", fact_key="reading",
                     content="主人爱读星际物理", evidence_event_id="evt_synthetic",
                     evidence_quote="爱读星际物理", **values)


def test_semantic_twin_and_attribute_reach_review_without_changing_answer_order():
    memory = Memory.__new__(Memory)
    memory.settings = SimpleNamespace(semantic_min_score=.5, lexical_keep_rank=3)
    ids = ["twin", *[f"noise_{i}" for i in range(7)]]
    rows = [{"memory_id": mid, "fact_key": mid, "content": mid} for mid in ids]
    rows.append({"memory_id": "attribute", "fact_key": "reading", "content": "old value"})
    scores = {mid: .99 - i * .01 for i, mid in enumerate(ids)}
    ordinary = memory._fused_order("new paraphrase", rows, ids[1:], scores)[0]
    review = memory._fused_order("new paraphrase", rows, ids[1:], scores,
                                 review=candidate())[0][:6]
    assert [r["memory_id"] for r in ordinary[:6]] == ids[1:7]
    assert {"twin", "attribute", "noise_0"} <= {r["memory_id"] for r in review}


def test_review_nominates_exact_attribute_with_live_sources_and_kind_scope(core, monkeypatch):
    mid = remember(core, 1, "我喜欢看宇宙学", fact_key="reading", confirmed=True)
    # Neither the lexical query nor an unavailable semantic channel nominates this old value.
    monkeypatch.setattr(core.memory.index, "nominate_async", _empty)
    monkeypatch.setattr(core.memory.index, "scores_async", _scores)
    result = run(core.memory.search_async("changed completely", subjects=["user"],
        now=core.clock(), clock=core.clock, review=candidate()))
    assert [r["memory_id"] for r in result] == [mid]
    assert result[0]["evidence_ids"] == ["evt_test_1"]
    core.memory.decide(mid, "forget", core.clock())
    assert not run(core.memory.search_async("changed completely", subjects=["user"],
        now=core.clock(), clock=core.clock, review=candidate()))


async def _empty(*args, **kwargs):
    return []


async def _scores(*args, **kwargs):
    kwargs["revalidate"]()
    return {}
