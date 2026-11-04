"""Write failures, semantic replacement targets and source-preserving selection."""

import json

import pytest

from companion.contracts import Candidate, CoreError
from companion.turn_recall import focus
from tests.conftest import ingest, run
from tests.test_m1 import remember


def update(core, old, *, key="讲座场地", resolution=None):
    text = "讲座更正到海盐厅，原来的苇灯厅不用了。"
    ingest(core, 2, text)
    candidate = Candidate(subject="project", kind="fact", fact_key=key, content=text,
                          evidence_event_id="evt_test_2", evidence_quote=text,
                          speaker="owner", update="replaces_previous")
    with core.store.transaction():
        return core.memory.add(candidate, core.clock(), resolution=resolution)


def test_reviewed_replacement_uses_canonical_target_before_key_lookup(core):
    old = remember(core, 1, "讲座在苇灯厅", subject="project", fact_key="lecture_venue")
    new = update(core, old, resolution={"same": None, "conflicts": [old], "replaces": [old]})
    rows = {r["memory_id"]: r for r in core.store.rows("SELECT * FROM memories")}
    assert rows[old]["valid_to"] == core.clock()
    assert rows[new]["fact_key"] == "lecture_venue"
    assert rows[new]["needs_review"] == 0


def test_reviewed_nonconflicting_attribute_survives_coarse_key_update(core):
    old = remember(core, 1, "朋友的弟弟做木偶道具", subject="project", fact_key="讲座场地")
    update(core, old, resolution={"supported": True, "update_authorized": False,
        "claims_replacement": False,
        "same": None, "conflicts": [], "replaces": []})
    assert core.store.one("SELECT valid_to FROM memories WHERE memory_id=?", (old,)) == {
        "valid_to": None}


@pytest.mark.parametrize("wrapped", [True, False])
def test_review_accepts_only_lossless_reference_shape(core, wrapped):
    old = remember(core, 1, "我喜欢绿茶", fact_key="drink")
    ingest(core, 2, "我不喜欢绿茶")
    candidate = Candidate(subject="user", kind="preference", fact_key="drink",
                          content="我不喜欢绿茶", evidence_event_id="evt_test_2",
                          evidence_quote="我不喜欢绿茶")
    core.model.review = lambda data: json.dumps({"relations": [
        {"supported": True, "update_authorized": False, "claims_replacement": False,
        "same": None,
        "conflicts": [{"ref": "m0"}] if wrapped else ["m0"], "replaces": []}]})
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    candidates, resolutions = run(core.prepare_memories([candidate]))
    assert candidates == [candidate]
    assert resolutions == [{"same": None, "conflicts": [old], "replaces": []}]


@pytest.mark.parametrize("refs", [[{"ref": "m0", "extra": True}], [{"ref": "m999"}],
                                  [{"ref": "m0"}, "m0"], [{"ref": 1}]])
def test_review_shape_repair_does_not_relax_source_or_schema_checks(core, refs):
    remember(core, 1, "我喜欢绿茶", fact_key="drink")
    ingest(core, 2, "我不喜欢绿茶")
    candidate = Candidate(subject="user", kind="preference", fact_key="drink",
                          content="我不喜欢绿茶", evidence_event_id="evt_test_2",
                          evidence_quote="我不喜欢绿茶")
    core.model.review = lambda data: json.dumps({"relations": [
        {"supported": True, "update_authorized": False, "claims_replacement": False,
        "same": None,
                                                               "conflicts": refs, "replaces": []}]})
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    with pytest.raises(CoreError, match="MEMORY_REVIEW_INVALID"):
        run(core.prepare_memories([candidate]))


def test_natural_changes_are_readable_from_every_version_without_owner_correction(core):
    old = remember(core, 1, "讲座在苇灯厅", subject="project", fact_key="lecture_venue")
    core.clock.advance(60)
    new = update(core, old, resolution={"same": None, "conflicts": [old], "replaces": [old]})
    for selected in (old, new):
        revisions = core.memory_context.history(selected, core.clock())["revisions"]
        assert [r["memory_id"] for r in revisions] == [new, old]
        assert revisions[0]["supersedes_memory_id"] is None
    assert [r["memory_id"] for r in core.memory_context.history(
        new, core.clock(), memory_ids={new})["revisions"]] == [new]
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    assert [r["memory_id"] for r in core.memory_context.history(
        new, core.clock())["revisions"]] == [new]


def test_history_uses_version_edges_when_batch_commit_times_are_equal(core, monkeypatch):
    identities = iter(["mem_earlier", "mem_later"])
    monkeypatch.setattr("companion.memory.uid", lambda prefix: next(identities))
    old = remember(core, 1, "讲座在苇灯厅", subject="project", fact_key="lecture_venue")
    new = update(core, old, resolution={"same": None, "conflicts": [old], "replaces": [old]})
    assert len({row["created_at"] for row in core.store.rows("SELECT * FROM memories")}) == 1
    # Reverse lexical IDs must not become the version order either.
    assert [row["memory_id"] for row in core.memory_context.history(
        old, core.clock())["revisions"]] == [new, old]


def test_attachment_rare_weekday_outranks_repeated_generic_table_term():
    text = "值守表\n人员 | 日期 | 地点\n" + "\n".join(
        f"值守{i}号 | 周一 | 大厅" for i in range(40)) + "\n杜嵩 | 周五14点 | 七号间"
    shown, cut = focus(text, "值守表周五什么时间去哪里找杜嵩", 300)
    assert cut and len(shown) <= 300
    assert "杜嵩 | 周五14点 | 七号间" in shown
    assert shown.startswith("值守表\n人员 | 日期 | 地点")


def test_recall_timeout_default_preserves_startup_identity_after_config_roundtrip(core):
    from companion.operator_config import _raw
    from companion.runtime_config import _cold

    assert type(core.settings.recall_compute_timeout_seconds) is float
    restored = type(core.settings).model_validate(_raw(core.settings))
    assert _cold(restored) == _cold(core.settings)


def test_unexplained_conflict_cannot_end_old_value_even_if_extractor_claims_update(core):
    old = remember(core, 1, "讲座在苇灯厅", subject="project", fact_key="lecture_venue")
    new = update(core, old, resolution={"same": None, "conflicts": [old], "replaces": []})
    assert core.store.one("SELECT valid_to FROM memories WHERE memory_id=?", (old,)) == {
        "valid_to": None}
    assert core.store.one("SELECT needs_review FROM memories WHERE memory_id=?", (new,)) == {
        "needs_review": 1}


def test_review_rejects_invented_identity_and_preserves_later_candidate_reference_indices(core):
    remember(core, 1, "我喜欢绿茶", fact_key="drink")
    texts = ["岑榆的弟弟叫岑聿，做木偶道具。", "我喜欢红茶", "我不喜欢红茶"]
    candidates = []
    for number, text in enumerate(texts, 2):
        ingest(core, number, text)
        candidates.append(Candidate(subject="user", kind="fact", fact_key="fact" + str(number),
            content=text + ("（岑榆即主人）" if number == 2 else ""),
            evidence_event_id=f"evt_test_{number}", evidence_quote=text))
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.model.review = lambda data: json.dumps({"relations": [
        {"supported": False, "update_authorized": False, "claims_replacement": False,
            "same": None, "conflicts": [], "replaces": []},
        {"supported": True, "update_authorized": False, "claims_replacement": False,
            "same": None, "conflicts": [], "replaces": []},
        {"supported": True, "update_authorized": False, "claims_replacement": False,
            "same": None, "conflicts": ["c1"], "replaces": []}]})
    accepted, resolutions = run(core.prepare_memories(candidates))
    assert accepted == candidates[1:]
    assert resolutions[1]["conflicts"] == ["c0"]
    with core.store.transaction():
        ids = core.add_memories(accepted, resolutions, core.clock())
    assert len(ids) == 2
    assert all("即主人" not in row["content"] for row in core.store.rows("SELECT * FROM memories"))


def test_review_original_excerpts_share_one_bounded_allowance(core, monkeypatch):
    for n in range(1, 11):
        remember(core, n, f"第{n}次记下的天文资料：" + "观测记录" * 70, fact_key=str(n))
    texts = ["我今天读了星图", "我明天继续看星图"]
    candidates = []
    for n, text in enumerate(texts, 20):
        ingest(core, n, text)
        candidates.append(Candidate(subject="user", kind="fact", fact_key=str(n), content=text,
            evidence_event_id=f"evt_test_{n}", evidence_quote=text))

    async def existing(*args, **kwargs):
        from companion.memory_context import MemorySources

        sources = MemorySources(core.memory, core.clock())
        return [dict(row, evidence_ids=[event["event_id"]
                                       for event in sources.evidence(row["memory_id"])])
                for row in core.store.rows("SELECT * FROM memories")]

    observed = []

    def review(payload):
        observed.append(payload)
        return json.dumps({"relations": [
            {"supported": True, "update_authorized": False, "claims_replacement": False,
                "same": None, "conflicts": [], "replaces": []}
            for _ in payload["candidates"]]})

    monkeypatch.setattr(core.memory, "search_async", existing)
    core.model.review = review
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    assert len(run(core.prepare_memories(candidates))[0]) == 2
    assert (sum(len(q) for row in observed[0]["existing"] for q in row["quotes"])
            + sum(len(row["source_excerpt"]) for row in observed[0]["candidates"])) == 2000


def test_lone_new_identity_claim_still_requires_evidence_review(core):
    core.settings.memory_enabled = True
    text = "汪赫的弟弟来这里学木偶道具。"
    ingest(core, 1, text)
    candidate = Candidate(subject="user", kind="fact", fact_key="name",
        content="主人是汪赫的弟弟", evidence_event_id="evt_test_1", evidence_quote=text)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.model.review = lambda data: json.dumps({"relations": [
        {"supported": False, "update_authorized": False, "claims_replacement": False,
            "same": None, "conflicts": [], "replaces": []}]})
    assert run(core.prepare_memories([candidate])) == ([], [])
    assert [kind for _, kind in core.model.calls] == ["memory_review"]


def test_explicit_replacement_key_survives_another_unresolved_conflict(core):
    old = remember(core, 1, "讲座在苇灯厅", subject="project", fact_key="lecture_venue")
    pending = remember(core, 3, "讲座在松蜜厅", subject="project", fact_key="讲座地点")
    new = update(core, old, resolution={"same": None, "conflicts": [old, pending],
                                       "replaces": [old]})
    row = core.store.one("SELECT * FROM memories WHERE memory_id=?", (new,))
    assert row["needs_review"] == 1
    assert row["fact_key"] == "lecture_venue"
    assert core.store.one("SELECT valid_to FROM memories WHERE memory_id=?", (pending,)) == {
        "valid_to": None}
    assert [r["memory_id"] for r in core.memory_context.history(
        old, core.clock())["revisions"]] == [new, old]


def test_explicit_replacement_family_keeps_older_attribute_key_aliases(core):
    first = remember(core, 1, "讲座在苇灯厅", subject="project", fact_key="lecture_venue")
    core.clock.advance(60)
    second = remember(core, 3, "讲座在松蜜厅", subject="project", fact_key="讲座地点")
    core.clock.advance(60)
    text = "我已核实讲座最终在海盐厅，之前说过的两个地点均作废。"
    ingest(core, 4, text)
    candidate = Candidate(subject="project", kind="fact", fact_key="lecture_place",
        content=text, evidence_event_id="evt_test_4", evidence_quote=text,
        speaker="owner", update="replaces_previous")
    new = core.memory.add(candidate, core.clock(), resolution={"same": None,
        "conflicts": [first, second], "replaces": [first, second]})
    for selected in (first, second, new):
        assert [r["memory_id"] for r in core.memory_context.history(
            selected, core.clock())["revisions"]] == [new, second, first]
