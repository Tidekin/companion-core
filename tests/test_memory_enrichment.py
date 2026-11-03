"""Semantic projections must follow native evidence, cost and erasure lifecycles."""

import json
from contextlib import closing

import pytest
from cryptography.fernet import Fernet

from companion import memory_enrichment as enrichment
from companion.backup import backup, restore
from companion.contracts import Candidate
from companion.memory import Memory
from companion.memory_context import MemoryContext
from companion.memory_work import checkpoint_state
from companion.model import ModelResponse
from companion.store import Store, after, encode
from tests.conftest import ingest, run
from tests.test_diary import DAY
from tests.test_nightly_stages import staged, step

IDS = [f"evt_test_{number}" for number in (1, 2, 3)]


def groups_by_event(core, payload):
    """Extractor input (short refs) as {event_id: its episode group's event IDs}."""
    real = {event["ref"]: core.store.one(
        "SELECT event_id FROM events WHERE json_extract(body_json,'$.text')=?",
        (event["text"],))["event_id"] for event in payload["events"]}
    return {real[event["ref"]]: [real[ref] for ref in payload["groups"].get(event["group"], [])]
            if event["group"] else [] for event in payload["events"]}


def prepared(core):
    model = staged(core)
    core.store.execute("DELETE FROM consolidation_runs")
    draft = json.loads(model.extraction)
    draft["memories"] = draft["memories"][:1]
    draft["memories"][0]["salience"] = 8
    draft["episode_summaries"] = [{
        "title": "先列章节的阅读计划",
        "summary": "交流围绕本周的阅读计划，用户想先读科幻，并习惯在开始前列出章节。",
        "source_event_ids": IDS,
    }]
    model.extraction = encode(draft)
    return model, draft


def committed(core):
    model, draft = prepared(core)
    assert run(core.extract_once())
    return model, draft, core.store.one("SELECT * FROM memories"), core.store.one(
        "SELECT * FROM episodes")


def add_support(core, model, draft):
    core.clock.advance(86400)
    candidate = {**draft["memories"][0], "evidence_event_id": "evt_test_4", "salience": 2}
    ingest(core, 4, candidate["content"])
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.settings.extraction_min_events = 1
    model.extraction = encode({"memories": [candidate]})
    assert run(core.extract_once())
    assert core.store.one("SELECT COUNT(*) AS n FROM memories")["n"] == 1
    return candidate


def test_summary_is_a_read_only_model_projection_and_repeat_evidence_is_one_vote(core):
    model, draft, fact, episode = committed(core)
    assert fact["importance"] == 0.8 and fact["status"] == "HYPOTHESIS"
    before = core.store.db.total_changes
    item = MemoryContext(core.memory).continuation(episode["conversation_id"], core.clock())[
        "items"][0]
    assert item["summary"]["text"] == draft["episode_summaries"][0]["summary"]
    assert item["summary"]["kind"] == "model_summary"
    assert item["source_event_ids"] == IDS and len(item["excerpts"]) == 3
    assert core.store.db.total_changes == before
    request_id = item["summary"]["request_id"]
    usage = core.store.rows("SELECT * FROM model_usage")
    with core.store.transaction():
        enrichment.record(core.memory, [Candidate.model_validate(draft["memories"][0])],
                          [fact["memory_id"]], core.store.rows("SELECT * FROM events"), [],
                          request_id, core.clock())
    saved = json.loads(core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                                      (enrichment.IMPORTANCE + fact["memory_id"],))["value"])
    assert len(saved) == 1
    assert core.store.one("SELECT importance FROM memories")["importance"] == 0.8
    assert core.store.rows("SELECT * FROM model_usage") == usage
    assert [kind for _, kind in model.calls] == ["memory_extract", "memory_review"]


@pytest.mark.parametrize("invalid", ["subset", "duplicate", "outside", "sensitive"])
def test_invalid_model_summary_never_commits_facts_or_derived_text(core, invalid):
    model, draft = prepared(core)
    item = draft["episode_summaries"][0]
    if invalid == "subset":
        item["source_event_ids"] = IDS[:2]
    elif invalid == "duplicate":
        item["source_event_ids"] = [*IDS, IDS[0]]
    elif invalid == "outside":
        item["source_event_ids"] = [*IDS, "evt_unseen"]
    else:
        item["summary"] = "密码是Secret123456"
    model.extraction = encode(draft)
    core.settings.max_attempts = 1
    assert not run(core.extract_once())
    assert checkpoint_state(core.store)["batch"]["phase"] == "FAILED"
    assert core.store.rows("SELECT * FROM memories") == []
    assert core.store.rows("SELECT * FROM episodes") == []
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    assert not run(core.extract_step()).progressed and len(model.calls) == 1


@pytest.mark.parametrize("invalid", ["source", "summary", "usage", "ids", "expiry"])
def test_summary_cannot_outlive_original_sources_content_or_usage(core, invalid):
    _, _, _, episode = committed(core)
    if invalid == "source":
        core.store.execute("UPDATE events SET body_json=? WHERE event_id='evt_test_1'",
                           (encode({"text": "changed"}),))
    elif invalid == "summary":
        core.store.execute("UPDATE episodes SET summary='unproven summary'")
    elif invalid == "usage":
        core.store.execute("UPDATE model_usage SET state='UNKNOWN'")
    elif invalid == "expiry":
        core.clock.advance((core.settings.raw_event_retention_days + 1) * 86400)
    before = core.store.db.total_changes
    assert core.memory.episode_summary(episode["episode_id"], IDS[:2] if invalid == "ids" else IDS,
                                       core.clock()) is None
    assert core.store.db.total_changes == before


@pytest.mark.parametrize("invalid", ["negative_support", "changed_fact", "unknown_usage"])
def test_invalid_estimates_cannot_raise_a_still_supported_fact(core, invalid):
    model, draft, fact, _ = committed(core)
    add_support(core, model, draft)
    if invalid == "negative_support":
        core.store.execute("UPDATE memory_evidence SET polarity=-1 WHERE event_id='evt_test_3'")
    elif invalid == "changed_fact":
        core.store.execute("UPDATE memories SET content='修改后的事实'")
    else:
        core.store.execute("UPDATE model_usage SET state='UNKNOWN' WHERE request_id=?", (
            json.loads(core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                (enrichment.IMPORTANCE + fact["memory_id"],))["value"])[0]["request_id"],))
    current = core.memory.search("*", now=core.clock())[0]
    assert current["importance"] == (0.5 if invalid == "changed_fact" else 0.2)
    assert current["status"] == "HYPOTHESIS" and not current["needs_review"]


def test_natural_retention_recomputes_surviving_score_and_replays_into_old_backup(core, tmp_path):
    model, draft, fact, episode = committed(core)
    add_support(core, model, draft)
    assert core.store.one("SELECT importance FROM memories")["importance"] == 0.5
    archive, restored = tmp_path / "before.enc", tmp_path / "restored.db"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    usage = core.store.rows("SELECT * FROM model_usage ORDER BY request_id")
    cutoff = after(core.clock(), -43200)
    with core.store.transaction():
        core.ledger.expire_events(core.store, IDS, core.clock(), retention_cutoff=cutoff)
    restore(archive, restored, key, core.settings.erasure_ledger)
    with closing(Store(str(restored))) as recovered:
        for store in (core.store, recovered):
            core.ledger.replay(store)
            memory = Memory(store, core.settings, core.ledger)
            current = memory.search("*", now=core.clock())[0]
            assert current["memory_id"] == fact["memory_id"] and current["importance"] == 0.2
            assert current["status"] == "HYPOTHESIS" and current["evidence_ids"] == ["evt_test_4"]
            assert memory.episode_summary(episode["episode_id"], IDS, core.clock()) is None
            assert not store.one("SELECT 1 FROM runtime_meta WHERE key=?",
                                  (enrichment.SUMMARY + episode["episode_id"],))
            assert not store.one("SELECT 1 FROM conversation_summaries WHERE summary_text=?",
                                  (draft["episode_summaries"][0]["summary"],))
            assert store.rows("SELECT * FROM model_usage ORDER BY request_id") == usage


@pytest.mark.parametrize("fault", ["withdraw", "unknown"])
def test_pending_enrichment_is_erased_or_stopped_without_replay(core, fault):
    model, draft = prepared(core)
    if fault == "unknown":
        original = model.complete

        async def missing_usage(messages, kind):
            response = await original(messages, kind)
            return ModelResponse(response.text)

        model.complete = missing_usage
        assert run(core.extract_step()).progressed
        state = checkpoint_state(core.store)["batch"]
        assert state["phase"] == "UNKNOWN" and "episode_summaries" not in state
    else:
        assert run(core.extract_step()).progressed
        state = checkpoint_state(core.store)["batch"]
        assert state["episode_summaries"] == draft["episode_summaries"]
        with core.store.transaction():
            core.ledger.expire_events(core.store, ["evt_test_3"], core.clock())
        assert checkpoint_state(core.store) is None
    usage = core.store.rows("SELECT * FROM model_usage")
    assert not run(core.extract_step()).progressed
    assert core.store.rows("SELECT * FROM memories") == []
    assert core.store.rows("SELECT * FROM episodes") == []
    assert core.store.rows("SELECT * FROM model_usage") == usage and len(model.calls) == 1


def test_salience_does_not_replace_relevance_or_confirm_a_fact(core):
    _, _, fact, _ = committed(core)
    assert core.memory.search("海水温度与珊瑚礁白化", now=core.clock()) == []
    assert core.store.one("SELECT status,importance FROM memories WHERE memory_id=?",
                          (fact["memory_id"],)) == {"status": "HYPOTHESIS", "importance": 0.8}


def test_late_input_gets_its_own_summary_without_replacing_the_n2_episode(core):
    model, draft = prepared(core)
    core.consolidation.schedule_day(DAY)
    assert step(core).progressed and step(core).progressed
    original = core.store.one("SELECT * FROM episodes")
    text = "还想比较不同译本。"
    ingest(core, 4, text, at=DAY.isoformat() + "T04:01:00+00:00")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    draft["episode_summaries"].append({
        "title": "比较译本", "summary": "用户另提出比较不同译本的计划。",
        "source_event_ids": ["evt_test_4"],
    })
    model.extraction = encode(draft)
    assert run(core.extract_once())
    events = groups_by_event(core, json.loads(model.calls[0][0][-1]["content"]))
    assert events == {
        **dict.fromkeys(IDS, IDS), "evt_test_4": ["evt_test_4"]}
    episodes = core.store.rows("SELECT * FROM episodes")
    assert len(episodes) == 2 and original["episode_id"] in {
        row["episode_id"] for row in episodes}
    for row in episodes:
        ids = [item["event_id"] for item in core.store.rows(
            "SELECT event_id FROM episode_events WHERE episode_id=? ORDER BY event_id",
            (row["episode_id"],))]
        assert core.memory.episode_summary(row["episode_id"], ids, core.clock())
    assert all(row["n"] == 1 for row in core.store.rows(
        "SELECT COUNT(*) AS n FROM episode_events GROUP BY event_id"))
    assert [kind for _, kind in model.calls] == ["memory_extract", "memory_review"]


def test_prior_episode_with_unprovided_source_cannot_be_resummarized(core):
    model, draft = prepared(core)
    prior = core.store.rows("SELECT * FROM events WHERE event_id IN (?,?)", IDS[:2])
    old_id = core.memory.episodes(prior, core.clock(), mark_processed=False)
    core.store.execute("UPDATE events SET memory_processed=1 WHERE event_id='evt_test_1'")
    core.settings.extraction_min_events = 1
    draft["episode_summaries"] = [{
        "title": "阅读习惯", "summary": "用户喜欢在阅读前先列出章节。",
        "source_event_ids": ["evt_test_3"],
    }]
    model.extraction = encode(draft)
    assert run(core.extract_once())
    events = groups_by_event(core, json.loads(model.calls[0][0][-1]["content"]))
    assert events == {
        "evt_test_2": [], "evt_test_3": ["evt_test_3"]}
    assert core.memory.episode_summary(old_id, IDS[:2], core.clock()) is None
    latest = core.store.one("SELECT episode_id FROM episode_events WHERE event_id='evt_test_3'")
    assert core.memory.episode_summary(latest["episode_id"], ["evt_test_3"], core.clock())
