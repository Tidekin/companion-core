"""Short refs in the extractor protocol, local bracket repair and the sensitivity split."""

import json

import pytest

from companion import memory_work
from companion.privacy import memory_sensitive, memory_summary_sensitive

DATA = [
    {"event_id": "evt_long_a", "conversation_id": "c", "occurred_at": "2026-01-01T01:00:00+00:00",
     "episode_source_event_ids": ["evt_long_a", "evt_long_b"], "text": "第一条",
     "references": [{"reference_id": "mref_long", "kind": "文件解析", "text": "表格",
                     "excerpt": False}]},
    {"event_id": "evt_long_b", "conversation_id": "c", "occurred_at": "2026-01-01T02:00:00+00:00",
     "episode_source_event_ids": ["evt_long_a", "evt_long_b"], "text": "第二条",
     "references": []},
]


def test_payload_lists_each_group_once_and_carries_no_long_ids():
    payload, mapping = memory_work.short_refs(DATA)
    text = json.dumps(payload, ensure_ascii=False)
    assert "evt_long" not in text and "mref_long" not in text
    assert payload["groups"] == {"g0": ["e0", "e1"]}
    assert [event["group"] for event in payload["events"]] == ["g0", "g0"]
    assert payload["events"][0]["references"][0]["ref"] == "r0"
    assert mapping == {"e0": "evt_long_a", "e1": "evt_long_b", "g0": ["evt_long_a", "evt_long_b"],
                       "r0": "mref_long"}


def test_short_refs_in_the_answer_map_back_before_validation():
    _, mapping = memory_work.short_refs(DATA)
    answer = json.dumps({"memories": [{
        "subject": "user", "kind": "fact", "fact_key": "k", "content": "主人说第一条",
        "evidence_event_id": "e0", "evidence_reference_id": "r0", "evidence_quote": "表格"}],
        "episode_summaries": [{"title": "t", "summary": "s", "source_event_ids": ["g0"]}],
        "interaction_signals": [{"event_id": "e1", "reason": "OWNER_PLAYFUL", "quote": "第二条"}]},
        ensure_ascii=False)
    extraction, repaired = memory_work.parse_extraction(answer, mapping)
    assert not repaired
    assert extraction.memories[0].evidence_event_id == "evt_long_a"
    assert extraction.memories[0].evidence_reference_id == "mref_long"
    assert extraction.episode_summaries[0].source_event_ids == ["evt_long_a", "evt_long_b"]
    assert extraction.interaction_signals[0].event_id == "evt_long_b"
    # Real IDs (older prompts, tests) and unknown refs pass through unchanged.
    value = memory_work.from_refs({"memories": [{"evidence_event_id": "evt_x"},
                                                {"evidence_event_id": "e9"}]}, mapping)
    assert [m["evidence_event_id"] for m in value["memories"]] == ["evt_x", "e9"]


def test_missing_closing_bracket_is_repaired_without_touching_strings():
    broken = ('{"memories":[],"episode_summaries":[{"title":"a]","summary":"s",'
              '"source_event_ids":["g0"}]}')
    with pytest.raises(ValueError):
        json.loads(broken)
    value = memory_work.repair_json(broken)
    assert value["episode_summaries"][0]["source_event_ids"] == ["g0"]
    assert value["episode_summaries"][0]["title"] == "a]"
    assert memory_work.repair_json('{"a": "unterminated') is None
    assert memory_work.repair_json('{"a": 1}}') is None


def test_summary_wording_alone_cannot_drop_a_fact():
    assert not memory_sensitive("今天去银行办了张卡。")
    assert memory_sensitive("我的银行卡号是6222")
    source = "今天去银行办了张卡。"
    assert not memory_summary_sensitive("主人今天去银行办了一张银行卡", source)
    for summary in ("主人可能有抑郁倾向", "主人住址在甲路", "主人密码是abc",
                    "主人的银行卡号6222", "主人的银行卡尾号1234"):
        assert memory_summary_sensitive(summary, source)
    assert memory_summary_sensitive("主人有一张银行卡", "今天吃面")


def recall_tools(core, monkeypatch, results, raw=None):
    """memory_recall over fixed recall results, with authority checks stubbed out."""
    import asyncio
    from types import SimpleNamespace

    from companion import agent_tools, reply_revision, turn_recall

    policy = SimpleNamespace(purposes={"chat", "memory", "history"},
                             history=SimpleNamespace(max_turns=14, max_utf8_bytes=32000))
    monkeypatch.setattr(agent_tools, "check_conversation", lambda *a, **k: SimpleNamespace(
        policy=policy))
    monkeypatch.setattr(reply_revision, "owner_input", lambda *a: True)
    monkeypatch.setattr(reply_revision, "remember_sources", lambda *a: None)
    calls = iter(results)

    async def recall_async(*args, **kwargs):
        return next(calls)

    context = SimpleNamespace(recall_async=recall_async,
                              pin=lambda *a, **k: {"source_event_ids": []})
    store = SimpleNamespace(one=lambda *a: {"ok": 1})
    fake = SimpleNamespace(store=store, settings=core.settings, clock=core.clock,
                           memory_context=context, memory=core.memory,
                           content=SimpleNamespace(options=SimpleNamespace(image_adapter="x")))
    core.settings.memory_enabled = True
    lookups = []

    async def prefetch(*args):
        lookups.append(args[-1])
        return {"first": ["evt_1"], "hop": []} if raw else None

    monkeypatch.setattr(turn_recall, "prefetch", prefetch)
    monkeypatch.setattr(turn_recall, "reference", lambda *a, **k: raw)
    tools = agent_tools.core_query_tools(fake, {"event_id": "e", "conversation_id": "c"}, [])
    run = lambda q: asyncio.run(tools.tools["memory_recall"].function(query=q))  # noqa: E731
    return run, lookups


FACT = {"memory_id": "mem_1", "content": "胶枪6个", "status": "HYPOTHESIS",
        "updated_at": "2026-01-01"}


def result(*facts):
    return {"state": "FOUND" if facts else "NO_RECORD", "facts": list(facts),
            "conflicts": [], "rows": []}


def test_repeated_memories_are_not_resent_and_recall_stops_without_progress(core, monkeypatch):
    run, lookups = recall_tools(core, monkeypatch, [result(FACT)] + [result(FACT)] * 2
                                + [result()] * 3)
    assert run("胶枪")["facts"][0]["content"] == "胶枪6个"
    second = run("胶枪数量")
    assert second["facts"] == [{"memory_id": "mem_1", "already_returned": True}]
    assert lookups == ["胶枪数量"]  # one raw-turn lookup, nothing found
    third = run("胶枪 几个")
    assert "note" in third
    assert run("胶枪 层")["state"] == "STOPPED"
    assert lookups == ["胶枪数量"]


def test_raw_turns_answer_once_when_summaries_have_nothing_new(core, monkeypatch):
    raw = {"data": {"turns": [{"owner_said": "今天去银行办了张卡。"}]}, "event_ids": ["evt_1"],
           "cost_bytes": 100}
    run, lookups = recall_tools(core, monkeypatch, [result()] * 3, raw=raw)
    first = run("办卡")
    assert first["raw_turns"]["turns"][0]["owner_said"] == "今天去银行办了张卡。"
    assert "raw_turns" not in run("银行")
    assert lookups == ["办卡"]


def test_changed_evidence_or_review_state_is_new_information(core, monkeypatch):
    changed = {**FACT, "needs_review": True, "evidence_ids": ["evt_new"]}
    run, _ = recall_tools(core, monkeypatch, [result(FACT), result(changed)])
    run("胶枪")
    assert run("胶枪数量")["facts"] == [changed]


def test_repaired_short_ref_extraction_commits_and_records_filter_reason(core):
    from tests.conftest import ingest, run

    core.settings.memory_enabled = True
    core.settings.mood_enabled = False
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    text = "今天去银行办了张卡。"
    key = ingest(core, 1, text)["event_id"]
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    candidate = {"subject": "user", "kind": "fact", "fact_key": "opened_card",
                 "content": "主人今天去银行办了一张银行卡", "evidence_event_id": "e0",
                 "evidence_quote": text}
    core.model.extraction = json.dumps({"memories": [candidate, {
        **candidate, "fact_key": "unsafe", "content": "主人住址在甲路"}]}, ensure_ascii=False)[:-1]
    assert run(core.extract_once())
    assert core.store.one("SELECT content FROM memories")["content"] == candidate["content"]
    assert [kind for _, kind in core.model.calls] == ["memory_extract", "memory_review"]
    funnel = core.store.one("SELECT safe_detail_json FROM audit_log "
                            "WHERE action='mood.appraisals_extracted'")
    assert json.loads(funnel["safe_detail_json"])["field_present"] is False
    assert core.store.rows("SELECT scenario,state FROM model_usage") == [
        {"scenario": "memory_extract", "state": "SETTLED"},
        {"scenario": "memory_review", "state": "SETTLED"}]
    assert core.store.one("SELECT entity_id FROM audit_log WHERE action='memory.output_repaired'")
    rejected = core.store.one("SELECT entity_id,safe_detail_json FROM audit_log "
                              "WHERE action='memory.candidate_rejected'")
    assert rejected["entity_id"] == key
    assert json.loads(rejected["safe_detail_json"]) == {"reason": "SENSITIVE_SUMMARY"}


@pytest.mark.parametrize("mood_enabled", [True, False])
def test_repaired_short_refs_preserve_appraisal_validation(core, mood_enabled):
    from tests.conftest import ingest, run

    core.settings.memory_enabled = True
    core.settings.mood_enabled = mood_enabled
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    key = ingest(core, 1, "想你了，什么时候再聊")["event_id"]
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.model.extraction = json.dumps({"memories": [], "interaction_signals": [
        {"event_id": "e0", "reason": "OWNER_AFFECTION", "quote": "想你了"},
    ]}, ensure_ascii=False)[:-1]

    assert run(core.extract_once())
    funnel = core.store.one("SELECT safe_detail_json FROM audit_log "
                            "WHERE action='mood.appraisals_extracted'")
    detail = json.loads(funnel["safe_detail_json"])
    assert detail["field_present"] is True and detail["returned"] == detail["kept"] == 1
    assert core.store.rows("SELECT event_id,reason FROM mood_appraisals") == (
        [{"event_id": key, "reason": "OWNER_AFFECTION"}] if mood_enabled else [])
    assert [kind for _, kind in core.model.calls] == ["memory_extract"]


def test_unrepairable_extraction_uses_only_existing_bounded_retries(core):
    from tests.conftest import ingest, run

    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    ingest(core, 1, "今天喝了绿茶")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.model.extraction = '{"memories":[{"content":"unfinished'
    for _ in range(core.settings.max_attempts + 1):
        run(core.extract_once())
        core.clock.advance(61)
    assert len(core.model.calls) == core.settings.max_attempts
    assert not core.store.rows("SELECT * FROM memories")
    assert core.store.one("SELECT memory_processed FROM events")["memory_processed"] == 1
