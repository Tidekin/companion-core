"""Source lifecycle and bounded hippocampal views over the native SQLite store."""
import json

import pytest
from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.contracts import Candidate, CoreError
from companion.core import Core
from companion.memory_context import MemoryContext
from companion.privacy import epoch
from companion.store import after
from tests.conftest import ingest
from tests.postgres_fixture import isolated_database, logical_state
from tests.support.confirmed_memory import remember
from tests.support.next_memory_context import context, episode


@pytest.mark.parametrize("invalid", ["negative", "event_scope", "conversation_scope",
                                     "conversation_owner", "tombstone", "upstream"])
def test_search_requires_positive_private_evidence_and_live_ancestry(core, invalid):
    mid = remember(core, 1, "项目约定是周二交接", confirmed=True)
    if invalid == "negative":
        core.store.execute("UPDATE memory_evidence SET polarity=-1")
    elif invalid == "event_scope":
        core.store.execute("UPDATE events SET privacy_scope='public'")
    elif invalid == "conversation_scope":
        core.store.execute("UPDATE conversations SET scope='group'")
    elif invalid == "conversation_owner":
        core.store.insert("principals", actor_id="someone_else", kind="owner",
                          created_at=core.clock())
        core.store.execute("UPDATE conversations SET owner_actor_id='someone_else'")
    elif invalid == "tombstone":
        core.store.execute("INSERT INTO runtime_meta VALUES ('erased_event:evt_test_1','erased')")
    else:
        ingest(core, 2, "上游原话")
        core.store.insert("event_dependencies", event_id="evt_test_1",
                          source_event_id="evt_test_2")
        core.store.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_2'")
    assert core.memory.search("*", now=core.clock()) == []
    assert context(core).history(mid, core.clock())["revisions"] == []


def test_expired_derived_copy_does_not_count_as_independent_support(core):
    view = context(core)
    core.settings.raw_event_retention_days = 3
    old = remember(core, 1, "常用饮料是绿茶", confirmed=True)
    core.clock.advance(2 * 86400)
    command = ingest(core, 2, f"/纠正这一条 {old} 常用饮料是红茶")
    revised = core.memory.decide(old, "correct", core.clock(), "常用饮料是红茶",
                                 evidence_event_id=command["event_id"])["memory_id"]
    # Simulate a recent materialization backed only by an expired raw correction.
    core.clock.advance(4 * 86400)
    core.store.execute("UPDATE events SET ingested_at=%s WHERE source='owner.decision'",
                       (core.clock(),))
    assert view.recall("红茶", core.clock())["state"] == "NO_RECORD"
    assert view.history(revised, core.clock())["revisions"] == []


def test_independent_support_survives_expiry_and_plan_never_mutates(core):
    view = context(core)
    core.settings.raw_event_retention_days = 3
    mid = remember(core, 1, "常用饮料是绿茶", confirmed=True)
    episode(core, 1)
    core.clock.advance(2 * 86400)
    assert remember(core, 2, "常用饮料是绿茶", confirmed=True) == mid
    core.clock.advance(2 * 86400)
    before = core.ledger.path.read_bytes(), logical_state(core.store), epoch(core.store)
    plan = view.invalidation_plan(["evt_test_1"], core.clock())
    assert plan["retain"] == [{"memory_id": mid, "evidence_ids": ["evt_test_2"]}]
    assert plan["withdraw"] == [] and len(plan["recompute_episode_ids"]) == 1
    assert before == (core.ledger.path.read_bytes(), logical_state(core.store), epoch(core.store))
    facts = view.recall("绿茶", core.clock())["facts"]
    assert [e["event_id"] for e in facts[0]["evidence"]] == ["evt_test_2"]
    assert view.invalidation_plan(["evt_test_2"], core.clock())["withdraw"] == [mid]


def test_correction_withdrawal_plan_includes_derived_and_legacy_copies(core):
    view = context(core)
    old = remember(core, 1, "项目交接在周二", confirmed=True)
    command = ingest(core, 2, f"/纠正这一条 {old} 项目交接在周三")
    new = core.memory.decide(old, "correct", core.clock(), "项目交接在周三",
                             evidence_event_id=command["event_id"])["memory_id"]
    derived = core.store.one("SELECT event_id FROM events WHERE source='owner.decision'")
    for legacy in (False, True):
        if legacy:
            core.store.execute("DELETE FROM event_dependencies")
        plan = view.invalidation_plan([command["event_id"]], core.clock())
        assert plan["source_event_ids"] == sorted([command["event_id"], derived["event_id"]])
        assert plan["withdraw"] == [new]
        assert old not in plan["withdraw"]


def test_episode_encoding_is_atomic_ordered_and_idempotent(core):
    view = context(core)
    ingest(core, 1, "松林项目已经画完草图")
    core.clock.advance(60)
    receipt = ingest(core, 2, "下次接着检查配色")
    eid = episode(core, 2, 1)
    assert episode(core, 1, 2) == eid
    assert core.store.one("SELECT COUNT(*) n FROM episodes")["n"] == 1
    assert core.store.one("SELECT COUNT(*) n FROM conversation_summaries")["n"] == 1
    assert all(row["memory_processed"] for row in core.store.rows("SELECT * FROM events"))
    conv = core.store.one('SELECT conversation_id FROM events WHERE event_id=%s',
                          (receipt["event_id"],))["conversation_id"]
    core.clock.advance(86400)
    item = view.continuation(conv, core.clock())["items"][0]
    assert [row["event_id"] for row in item["excerpts"]] == ["evt_test_1", "evt_test_2"]
    assert item["excerpts"][-1]["excerpt"] == "下次接着检查配色"
    assert view.continuation(conv, core.clock(), event_ids=["evt_test_2"])["items"] == []
    assert view.continuation("another-conversation", core.clock())["items"] == []


@pytest.mark.parametrize("invalid", ["mixed", "changed", "redacted", "expired"])
def test_episode_rejects_invalid_sources_without_partial_commit(core, invalid):
    context(core)
    ingest(core, 1, "合成对话一")
    ingest(core, 2, "合成对话二", origin="another:FriendMessage:10001" if invalid == "mixed"
           else None)
    events = core.store.rows("SELECT * FROM events")
    if invalid == "changed":
        core.store.execute("UPDATE events SET body_json=%s WHERE event_id='evt_test_2'",
                           (json.dumps({"text": "新的正文"}),))
    elif invalid == "redacted":
        core.store.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_2'")
    elif invalid == "expired":
        core.clock.advance((core.settings.raw_event_retention_days + 1) * 86400)
    with pytest.raises(CoreError, match="EPISODE_SOURCES|EPISODE_SOURCE_CHANGED"):
        core.memory.episodes(events, core.clock())
    assert not core.store.rows("SELECT * FROM episodes")
    assert not any(row["memory_processed"] for row in core.store.rows("SELECT * FROM events"))


@pytest.mark.parametrize("text,expected", [("/你记得什么", False), ("/随便聊聊 天文", True)])
def test_internal_episode_filters_controls_but_preserves_ordinary_slash_text(core, text, expected):
    view = context(core)
    ingest(core, 1, text)
    episode(core, 1)
    conv = core.store.one("SELECT conversation_id FROM conversations")
    assert bool(view.continuation(conv["conversation_id"], core.clock())["items"]) is expected


def test_continuation_rebuilds_from_valid_sources_and_respects_bounds(core):
    view = context(core)
    for n in range(1, 14):
        ingest(core, n, f"第{n}条公开合成进度，" + "字" * 250)
        core.clock.advance(1)
    for start in (1, 4, 7, 10):
        episode(core, *range(start, start + 3))
    conv = core.store.one("SELECT conversation_id FROM conversations")["conversation_id"]
    result = view.continuation(conv, core.clock())
    assert len(result["items"]) == 3 and result["truncated"]
    assert all(len(excerpt["excerpt"]) <= 200 for item in result["items"]
               for excerpt in item["excerpts"])
    # A stale stored summary must never be used as an alternative source.
    core.store.execute("UPDATE episodes SET summary='stale summary'")
    core.store.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_10'")
    result = view.continuation(conv, core.clock())
    assert "stale summary" not in json.dumps(result)
    assert "evt_test_11" not in json.dumps(result)
    core.clock.advance(8 * 86400)
    assert view.continuation(conv, core.clock())["items"] == []


def test_conflicts_separate_from_facts_and_owner_correction_is_current(core):
    view = context(core)
    old = remember(core, 1, "项目交接在周二", fact_key="handoff", confirmed=True)
    remember(core, 2, "项目交接在周三", fact_key="handoff")
    result = view.recall("交接", core.clock())
    assert result["state"] == "NEEDS_REVIEW" and not result["facts"]
    assert len(result["conflicts"]) == 2
    core.clock.advance(86400)
    revised = core.memory.decide(old, "correct", core.clock(), "项目交接改为周四")["memory_id"]
    result = view.recall("交接", core.clock())
    assert [fact["memory_id"] for fact in result["facts"]] == [revised]
    history = view.history(revised, core.clock())["revisions"]
    assert {row["memory_id"] for row in history} == {old, revised}
    assert all(row["valid_from"] is None for row in history)  # no invented effective dates
    assert {row["evidence"][0]["occurred_at"] for row in history} == {
        after(core.clock(), -86400), core.clock(),
    }
    assert [row["memory_id"] for row in view.history(
        revised, core.clock(), memory_ids=[revised],
    )["revisions"]] == [revised]


def test_fact_validity_is_explicit_and_half_open(core):
    view = context(core)
    mid = remember(core, 1, "本周交接点在东门", confirmed=True)
    core.store.execute('UPDATE memories SET valid_from=%s,valid_to=%s WHERE memory_id=%s',
                       (after(core.clock(), 60), after(core.clock(), 120), mid))
    assert view.recall("交接点", core.clock())["state"] == "NO_RECORD"
    core.clock.advance(60)
    assert view.recall("交接点", core.clock())["state"] == "FOUND"
    core.clock.advance(60)
    assert view.recall("交接点", core.clock())["state"] == "NO_RECORD"
    assert len(view.history(mid, core.clock())["revisions"]) == 1


def test_no_record_is_distinct_from_unavailable_and_scope_denial(core, monkeypatch):
    view = context(core)
    assert view.recall("没记录的宠物名", core.clock())["state"] == "NO_RECORD"
    mid = remember(core, 1, "项目约定是周二交接", confirmed=True)
    assert view.recall("项目约定", core.clock(), memory_ids=[])["state"] == "NO_RECORD"
    with pytest.raises(CoreError, match="MEMORY_ACCESS_DENIED"):
        view.history(mid, core.clock(), actor="another-user")

    def unavailable(*args):
        raise CoreError("MEMORY_INDEX_UNAVAILABLE", 503)

    monkeypatch.setattr(core.memory.index, "scores", unavailable)
    with pytest.raises(CoreError, match="MEMORY_INDEX_UNAVAILABLE"):
        view.recall("项目约定", core.clock())


def test_forget_during_embedding_cannot_return_stale_fact(core, monkeypatch):
    view = context(core)
    mid = remember(core, 1, "项目约定是周二交接", confirmed=True)

    def erase(query, rows):
        core.memory.decide(mid, "forget", core.clock())
        return {row["memory_id"]: 1.0 for row in rows}

    monkeypatch.setattr(core.memory.index, "scores", erase)
    with pytest.raises(CoreError, match="PRIVACY_CHANGED"):
        view.recall("项目约定", core.clock())


def test_ledger_first_failure_blocks_views_until_replay(core, monkeypatch):
    view = context(core)
    mid = remember(core, 1, "项目约定是周二交接", confirmed=True)
    with monkeypatch.context() as patch:
        def interrupted(*args):
            raise RuntimeError("synthetic apply failure")
        patch.setattr(core.ledger, "apply", interrupted)
        with pytest.raises(RuntimeError, match="synthetic"):
            core.memory.decide(mid, "forget", core.clock())
    for operation in (lambda: view.recall("项目约定", core.clock()),
                      lambda: view.history(mid, core.clock()),
                      lambda: view.invalidation_plan(["evt_test_1"], core.clock())):
        with pytest.raises(CoreError, match="ERASURE_REPLAY_REQUIRED"):
            operation()
    core.ledger.replay(core.store)
    assert view.recall("项目约定", core.clock())["state"] == "NO_RECORD"


def test_forget_and_old_backup_replay_remove_facts_revisions_and_continuation(core, tmp_path):
    view = context(core)
    old = remember(core, 1, "项目约定是周二交接", confirmed=True)
    episode(core, 1)
    other = remember(core, 2, "独立事实是爱吃苹果", confirmed=True)
    new = core.memory.decide(old, "correct", core.clock(), "项目约定是周三交接")["memory_id"]
    archive = tmp_path / "before.enc"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    core.memory.decide(new, "forget", core.clock())
    with isolated_database() as destination:
        state = tmp_path / 'restored-state'
        restore(archive, destination, key, core.settings.erasure_ledger, state_directory=state)
        settings = core.settings.model_copy(update={'database':destination,
                                                    'state_directory':str(state)})
        with Core(settings, core.model, core.clock) as reopened:
            for service in (view, MemoryContext(reopened.memory)):
                assert service.recall("交接", core.clock())["state"] == "NO_RECORD"
                assert service.history(new, core.clock())["revisions"] == []
                assert service.recall("苹果", core.clock())["facts"][0]["memory_id"] == other
                conv = service.store.one(
                    "SELECT conversation_id FROM conversations WHERE scope='private'"
                )["conversation_id"]
                assert service.continuation(conv, core.clock())["items"] == []


def test_candidate_cannot_use_expired_or_nonprivate_source(core):
    context(core)
    source = ingest(core, 1, "项目约定是周二交接")
    candidate = Candidate(subject="project", kind="fact", fact_key="handoff",
                          content="项目约定是周二交接", evidence_quote="项目约定是周二交接",
                          evidence_event_id=source["event_id"])
    core.clock.advance((core.settings.raw_event_retention_days + 1) * 86400)
    with pytest.raises(CoreError, match="MISSING_MEMORY_EVIDENCE"):
        core.memory.add(candidate, core.clock())
    assert not core.store.rows("SELECT * FROM memories")


def test_delayed_extraction_cannot_undo_owner_correction(core):
    view = context(core)
    old = remember(core, 1, "项目交接在周二", fact_key="handoff", confirmed=True)
    core.clock.advance(60)
    new = core.memory.decide(old, "correct", core.clock(), "项目交接在周三")["memory_id"]
    candidate = Candidate(subject="user", kind="fact", fact_key="handoff",
                          content="项目交接在周二", evidence_quote="项目交接在周二",
                          evidence_event_id="evt_test_1")
    assert core.memory.add(candidate, core.clock()) is None
    assert core.store.one("SELECT COUNT(*) n FROM memories")["n"] == 2
    # A later inferred contrary observation remains pending, without downgrading the correction.
    core.clock.advance(60)
    ingest(core, 2, "项目交接在周四")
    candidate = candidate.model_copy(update={"content": "项目交接在周四",
        "evidence_quote": "项目交接在周四", "evidence_event_id": "evt_test_2"})
    pending = core.memory.add(candidate, core.clock(),
                              resolution={"same": None, "conflicts": [new]})
    result = view.recall("交接", core.clock())
    assert [row["memory_id"] for row in result["facts"]] == [new]
    assert [row["memory_id"] for row in result["conflicts"]] == [pending]
    core.clock.advance(60)
    ingest(core, 3, "周三是项目交接时间")
    candidate = candidate.model_copy(update={"content": "周三是项目交接时间",
        "evidence_quote": "周三是项目交接时间", "evidence_event_id": "evt_test_3"})
    assert core.memory.add(candidate, core.clock(),
                           resolution={"same": new, "conflicts": [pending]}) == new
    assert core.store.one('SELECT needs_review FROM memories WHERE memory_id=%s',
                          (new,))["needs_review"] == 0


def test_source_withdrawal_read_contract_retains_only_independent_support(core):
    view = context(core)
    shared = remember(core, 1, "常用饮料是绿茶", confirmed=True)
    only = remember(core, 2, "唯一来源的项目暗号是青竹", confirmed=True)
    episode(core, 1, 2)
    core.clock.advance(60)
    assert remember(core, 3, "常用饮料是绿茶", confirmed=True) == shared
    # The integration owner supplies input_recall + Ledger replay. This tests its read contract.
    with core.store.transaction():
        for key in ("evt_test_1", "evt_test_2"):
            core.store.execute("UPDATE events SET redacted=1,body_json='{}' WHERE event_id=%s",
                               (key,))
            core.store.execute('INSERT INTO runtime_meta VALUES (%s,%s)',
                               ("erased_event:" + key, "synthetic-input-recall"))
    found = view.recall("绿茶", core.clock())["facts"]
    assert found[0]["memory_id"] == shared
    assert [proof["event_id"] for proof in found[0]["evidence"]] == ["evt_test_3"]
    assert view.recall("青竹", core.clock())["state"] == "NO_RECORD"
    assert view.history(only, core.clock())["revisions"] == []
    conv = core.store.one("SELECT conversation_id FROM conversations")
    assert view.continuation(conv["conversation_id"], core.clock())["items"] == []
