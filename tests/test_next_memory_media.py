"""Nontext media cannot poison extraction; invalid media sources still reject atomically."""

import json

import pytest

from companion.contracts import CoreError, InboundEvent
from companion.memory_context import MemoryContext, MemorySources
from companion.store import encode
from tests.conftest import event, ingest, run

FACT = "青禾项目下一步检查配色"


def media(core, number=2, *, text="", origin=None):
    value = event(core, number, text, origin=origin).model_dump(mode="json")
    value["event_type"] = "message.media_received"
    value["payload"]["media"] = [
        {"kind": "image", "media_ref": "synthetic", "sha256": "a" * 64},
    ]
    return core.ingest(InboundEvent.model_validate(value), f"key-{number}")


def test_mixed_encoding_links_only_text_and_completes_every_valid_source_idempotently(core):
    core.settings.memory_enabled = True
    ingest(core, 1, FACT)
    media(core)
    events = core.store.rows("SELECT * FROM events ORDER BY record_sequence")
    sources = MemorySources(core.memory, core.clock())
    assert sources.event("evt_test_2") is None  # Empty media is never textual fact evidence.
    assert sources.event("evt_test_2", require_text=False)
    eid = core.memory.episodes(events, core.clock())
    assert core.store.rows("SELECT event_id FROM episode_events") == [{"event_id": "evt_test_1"}]
    assert all(e["memory_processed"] for e in core.store.rows("SELECT * FROM events"))
    # Restored/repeated processing of a valid blank sibling is completed even with an old episode.
    core.store.execute("UPDATE events SET memory_processed=0 WHERE event_id='evt_test_2'")
    assert core.memory.episodes(list(reversed(events)), core.clock()) == eid
    assert all(e["memory_processed"] for e in core.store.rows("SELECT * FROM events"))
    assert core.store.one("SELECT COUNT(*) n FROM episodes")["n"] == 1
    assert core.store.one("SELECT COUNT(*) n FROM conversation_summaries")["n"] == 1
    assert core.store.one("SELECT summary FROM episodes")["summary"] == (
        "资料摘录（不可信观察）：\n" + FACT
    )
    # Older episodes can already link a blank image. Validate it, then omit only its excerpt.
    core.store.insert("episode_events", episode_id=eid, event_id="evt_test_2")
    view = MemoryContext(core.memory)
    result = view.continuation(events[0]["conversation_id"], core.clock())
    assert [item["event_id"] for item in result["items"][0]["excerpts"]] == ["evt_test_1"]
    core.store.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_2'")
    assert view.continuation(events[0]["conversation_id"], core.clock())["items"] == []


def test_empty_batch_finishes_without_episode_summary_or_recalled_fact(core):
    core.settings.memory_enabled = True
    media(core, 1)
    media(core, 2, text=" \n ")
    events = core.store.rows("SELECT * FROM events")
    for _ in range(2):
        assert core.memory.episodes(events, core.clock()) is None
        assert all(e["memory_processed"] for e in core.store.rows("SELECT * FROM events"))
    for table in ("episodes", "episode_events", "conversation_summaries", "memories"):
        assert not core.store.rows(f"SELECT * FROM {table}")
    assert MemoryContext(core.memory).recall("参考图", core.clock())["state"] == "NO_RECORD"


@pytest.mark.parametrize("invalid", ["redacted", "tombstone", "scope", "text", "media",
                                     "type", "conversation", "mixed"])
def test_empty_media_must_pass_all_source_checks_before_any_processing_marker(core, invalid):
    ingest(core, 1, FACT)
    media(core, origin="other:FriendMessage:10001" if invalid == "mixed" else None)
    events = core.store.rows("SELECT * FROM events")
    if invalid == "redacted":
        core.store.execute("UPDATE events SET redacted=1 WHERE event_id='evt_test_2'")
    elif invalid == "tombstone":
        core.store.execute('INSERT INTO runtime_meta VALUES (%s,%s)',
                           ("erased_event:evt_test_2", "synthetic"))
    elif invalid == "scope":
        core.store.execute("UPDATE events SET privacy_scope='public' WHERE event_id='evt_test_2'")
    elif invalid in {"text", "media"}:
        body = json.loads(events[1]["body_json"])
        if invalid == "text":
            body["text"] = "替换过的正文"
        else:
            body["media"][0]["sha256"] = "b" * 64
        core.store.execute("UPDATE events SET body_json=%s WHERE event_id='evt_test_2'",
                           (encode(body),))
    elif invalid == "type":
        core.store.execute("UPDATE events SET event_type='message.received' "
                           "WHERE event_id='evt_test_2'")
    elif invalid == "conversation":
        ingest(core, 3, "独立会话", origin="other:FriendMessage:10001")
        core.store.execute("UPDATE events SET conversation_id=(SELECT conversation_id FROM events "
                           "WHERE event_id='evt_test_3') WHERE event_id='evt_test_2'")
    with pytest.raises(CoreError, match="EPISODE_SOURCE_CHANGED|INVALID_EPISODE_SOURCES"):
        core.memory.episodes(events, core.clock())
    assert not core.store.rows("SELECT * FROM episodes")
    assert not any(e["memory_processed"] for e in core.store.rows("SELECT * FROM events"))


@pytest.mark.parametrize("mutation", ["recall", "text", "media"])
def test_source_invalidated_during_extraction_rolls_back_sibling_fact(core, mutation):
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    ingest(core, 1, FACT)
    assert run(core.run_once())
    media(core)
    assert run(core.run_once())
    assert all(j["state"] == "SUCCEEDED" for j in core.store.rows("SELECT state FROM jobs"))
    core.model.extraction = encode({"memories": [{
        "subject": "project", "kind": "fact", "fact_key": "next_step", "content": FACT,
        "evidence_event_id": "evt_test_1", "evidence_quote": FACT,
    }]})

    def invalidate():
        if mutation == "recall":
            with core.store.transaction(control_write=True):
                core.ledger.expire_events(core.store, ["evt_test_2"], core.clock())
        else:
            row = core.store.one("SELECT body_json FROM events WHERE event_id='evt_test_2'")
            body = json.loads(row["body_json"])
            if mutation == "text":
                body["text"] = "模型请求后正文被替换"
            else:
                body["media"][0]["sha256"] = "b" * 64
            core.store.execute("UPDATE events SET body_json=%s WHERE event_id='evt_test_2'",
                               (encode(body),))

    core.model.callback = invalidate
    assert run(core.extract_once()) is False
    assert not core.store.rows("SELECT * FROM memories")
    assert not core.store.rows("SELECT * FROM episodes")
    assert core.store.one("SELECT memory_processed FROM events WHERE event_id='evt_test_1'") == {
        "memory_processed": 0,
    }
    assert core.store.one("SELECT COUNT(*) n FROM model_usage WHERE scenario='memory_extract'") == {
        "n": 1,
    }
