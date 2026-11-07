"""QQ memory controls retain their actual source without becoming chat/extraction input."""

import json

import pytest
from cryptography.fernet import Fernet

from companion import dialogue_history
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.privacy import epoch
from companion.store import Store, encode
from tests.conftest import ingest, run
from tests.support.memory_commands import fact


@pytest.mark.parametrize(
    "command",
    [
        "/记住这个：本次项目代号是青竹七号。",
        "/你记得什么",
        "/确认这一条 mem_1234",
        "/纠正这一条 mem_1234 本次项目代号是白帆九号。",
        "/忘记这一条 mem_1234",
    ],
)
def test_memory_controls_are_not_extracted_or_included_as_recent_dialogue(core, command):
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    receipt = ingest(core, 1, command)
    source = core.store.one("SELECT * FROM events WHERE event_id=?", (receipt["event_id"],))
    with core.store.transaction():
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?", (receipt["job_id"],))
        message_id = core.expression.enqueue(
            source["event_id"],
            source["conversation_id"],
            "合成控制结果",
            core.clock(),
        )
        core.store.execute("UPDATE outbox SET state='SENT' WHERE message_id=?", (message_id,))
    current = ingest(core, 2, "新的独立问题")
    # Exercise actual consumers, rather than relying only on the marker's shape.
    assert run(core.extract_once()) is False
    assert core.model.calls == []
    assert (
        dialogue_history.recent_sent_sources(
            core.store,
            source["conversation_id"],
            current["event_id"],
            4,
        )
        == []
    )
    assert source["memory_processed"] == 1
    assert json.loads(source["body_json"])["control_command"] is True


@pytest.mark.parametrize("text", ["记住这个：普通谈话", "我想/忘记这一条", "/记住这个吗 内容"])
def test_similar_ordinary_text_is_not_a_memory_control(core, text):
    receipt = ingest(core, 1, text)
    source = core.store.one("SELECT * FROM events WHERE event_id=?", (receipt["event_id"],))
    assert not source["memory_processed"]
    assert not json.loads(source["body_json"]).get("control_command")


@pytest.mark.parametrize("separator", [" ", "\t", "\n", "\u3000"])
def test_forget_and_latest_ledger_replay_erase_the_actual_qq_correction_only(
    core, tmp_path, separator
):
    old = fact(core, 1, "本次项目代号是青竹七号。")
    independent = fact(core, 2, "另一独立项目的颜色是蓝色。")
    correction = ingest(core, 3, f"/纠正这一条 {old}{separator}本次项目代号是白帆九号。")
    assert run(core.run_once())
    revised = core.store.one("SELECT * FROM memories WHERE supersedes_memory_id=?", (old,))
    assert revised["content"] == "本次项目代号是白帆九号。"
    assert core.model.calls == []
    key, snapshot, restored = Fernet.generate_key(), tmp_path / "before.enc", tmp_path / "after.db"
    backup(core.settings.database, snapshot, key)
    before = epoch(core.store)
    forget = ingest(core, 4, f"/忘记这一条 {revised['memory_id']}")
    assert run(core.run_once())
    assert epoch(core.store) > before
    assert core.store.one("SELECT state FROM jobs WHERE job_id=?", (forget["job_id"],)) == {
        "state": "SUCCEEDED",
    }
    restore(snapshot, restored, key, core.settings.erasure_ledger)
    reopened = Store(str(restored))
    try:
        for store in (core.store, reopened):
            row = store.one(
                "SELECT redacted,body_json FROM events WHERE event_id=?", (correction["event_id"],)
            )
            assert row == {"redacted": 1, "body_json": "{}"}
            assert store.one(
                "SELECT content,status FROM memories WHERE memory_id=?", (independent,)
            ) == {
                "content": "另一独立项目的颜色是蓝色。",
                "status": "CONFIRMED",
            }
            assert (
                store.one("SELECT redacted FROM events WHERE event_id='evt_test_2'")["redacted"]
                == 0
            )
            assert store.rows("SELECT * FROM memory_revisions") == []
            assert "白帆九号" not in encode(store.rows("SELECT body_json FROM events"))
            assert store.one("SELECT COUNT(*) AS n FROM model_usage")["n"] == 0
    finally:
        reopened.close()


@pytest.mark.parametrize("decision,command", [("confirm", "确认这一条"), ("forget", "忘记这一条")])
def test_factless_controls_have_safe_source_audit_without_becoming_fact_evidence(
    core, decision, command
):
    memory_id = fact(core, 1, "本次项目代号是青竹七号。")
    receipt = ingest(core, 2, f"/{command} {memory_id}")
    assert run(core.run_once())
    audit = core.store.one("SELECT * FROM audit_log WHERE action='memory.owner_decision'")
    assert json.loads(audit["safe_detail_json"]) == {
        "decision": decision,
        "source_event_id": receipt["event_id"],
    }
    assert audit["trace_id"] == "trace_test_2"
    assert (
        core.store.one("SELECT 1 FROM memory_evidence WHERE event_id=?", (receipt["event_id"],))
        is None
    )
    assert (
        core.store.one("SELECT redacted FROM events WHERE event_id=?", (receipt["event_id"],))[
            "redacted"
        ]
        == 0
    )
    assert (
        core.store.one("SELECT state FROM jobs WHERE job_id=?", (receipt["job_id"],))["state"]
        == "SUCCEEDED"
    )
    assert core.model.calls == []


@pytest.mark.parametrize("change", ["redacted", "actor", "target", "content", "source", "private"])
def test_qq_correction_source_must_match_the_authenticated_operation(core, change):
    memory_id = fact(core, 1, "本次项目代号是青竹七号。")
    text = f"/纠正这一条 {memory_id} 本次项目代号是白帆九号。"
    receipt = ingest(core, 2, text)
    columns = {
        "redacted": ("redacted", 1),
        "actor": ("actor_id", "companion"),
        "target": ("body_json", encode({"text": "/纠正这一条 mem_ffff 本次项目代号是白帆九号。"})),
        "content": ("body_json", encode({"text": text + "另一个值"})),
        "source": ("source", "owner.decision"),
        "private": ("privacy_scope", "public"),
    }
    column, value = columns[change]
    core.store.execute(
        f"UPDATE events SET {column}=? WHERE event_id=?", (value, receipt["event_id"])
    )
    before = epoch(core.store)
    with pytest.raises(CoreError, match="MEMORY_DECISION_SOURCE_INVALID"):
        core.memory.decide(
            memory_id,
            "correct",
            core.clock(),
            "本次项目代号是白帆九号。",
            evidence_event_id=receipt["event_id"],
        )
    assert epoch(core.store) == before
    assert (
        core.store.one("SELECT status FROM memories WHERE memory_id=?", (memory_id,))["status"]
        == "CONFIRMED"
    )
    assert core.store.one("SELECT COUNT(*) AS n FROM memories")["n"] == 1
