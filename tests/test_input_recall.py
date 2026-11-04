"""Owner inbound withdrawal, evidence independence and external Ledger replay."""

from contextlib import closing

import pytest
from cryptography.fernet import Fernet

import companion.affect as affect
from companion import budget
from companion.backup import backup, restore
from companion.contracts import Candidate, CoreError, InboundEvent
from companion.input_recall import (
    canonical_source,
    is_recalled,
    marker_key,
    recall_input,
    require_not_recalled,
)
from companion.memory import Memory
from companion.privacy import epoch
from companion.store import Store, after, encode
from companion.transport import enqueue
from tests.support.onebot_events import config, normalized, raw_event


def source(number=42, **changes):
    return canonical_source(
        **{
            "owner_qq_id": "10001",
            "bot_qq_id": "10002",
            "protocol_epoch": config().protocol_epoch,
            "provider_message_id": str(number),
            **changes,
        }
    )


def incoming(core, number=42, text="我喜欢绿茶", *, event_id=None):
    value = normalized(raw_event(number, text))
    if event_id is not None:
        value["event_id"] = event_id
    event = InboundEvent.model_validate(value)
    core.ingest(event, event.event_id)
    return event.event_id


def remember(core, event_id, text="我喜欢绿茶"):
    core.settings.memory_enabled = True
    with core.store.transaction():
        return core.memory.add(
            Candidate(
                subject="user",
                kind="preference",
                fact_key="饮料",
                content=text,
                evidence_event_id=event_id,
                evidence_quote=text,
            ),
            core.clock(),
            confirmed=True,
        )


def recall(core, number=42, *, verified=True):
    return recall_input(
        core.store,
        core.ledger,
        source(number),
        core.clock(),
        retention_cutoff=after(core.clock(), -86400 * 30),
        verified=verified,
        occurred_at=core.clock(),
        event_id=normalized(raw_event(number))["event_id"] if verified else None,
    )


def test_before_input_marker_is_idempotent_scoped_and_replayed_from_pre_notice_backup(
    core,
    tmp_path,
):
    archive, destination, key = (
        tmp_path / "before.enc",
        tmp_path / "restore.db",
        Fernet.generate_key(),
    )
    backup(core.settings.database, archive, key)
    previous_epoch = epoch(core.store)
    first = recall(core, verified=False)
    assert first["state"] == "PENDING_TARGET" and first["event_ids"] == []
    assert not first["duplicate"]
    assert recall(core, verified=False) == {**first, "duplicate": True}
    assert len(core.ledger.read()) == 2
    assert epoch(core.store) == previous_epoch
    restore(archive, destination, key, core.settings.erasure_ledger)
    with closing(Store(str(destination))) as restored:
        for store in (core.store, restored):
            require_not_recalled(store, source()["source_event_id"])
            assert not is_recalled(store, source()["source_event_id"])
            for unrelated in (
                source(43),
                source(protocol_epoch="another-epoch"),
                source(owner_qq_id="10003"),
                source(bot_qq_id="10004"),
            ):
                require_not_recalled(store, unrelated["source_event_id"])
            assert not store.rows("SELECT * FROM events")
            completed_epoch = epoch(store)
            core.ledger.replay(store)
            core.ledger.replay(store)
            assert epoch(store) == completed_epoch


def test_verified_before_input_recall_and_pending_projection_never_downgrade_after_restore(
    core,
    tmp_path,
):
    recall(core, verified=False)
    archive, destination, key = (
        tmp_path / "pending.enc",
        tmp_path / "restore.db",
        Fernet.generate_key(),
    )
    backup(core.settings.database, archive, key)
    expected = recall(core)
    assert expected["state"] == "RECALLED"
    assert expected["event_ids"] == [normalized()["event_id"]]
    assert recall(core, verified=False) == {**expected, "duplicate": True}
    assert len(core.ledger.read()) == 3
    restore(archive, destination, key, core.settings.erasure_ledger)
    with closing(Store(str(destination))) as restored:
        for store in (core.store, restored):
            with pytest.raises(CoreError, match="EVENT_ERASED"):
                require_not_recalled(store, source()["source_event_id"])
            assert is_recalled(store, normalized()["event_id"])
            store.execute(
                "DELETE FROM runtime_meta WHERE key=?", (marker_key(source()["source_event_id"]),)
            )
            core.ledger.replay(store)
            assert is_recalled(store, source()["source_event_id"])


def test_unknown_recall_never_erases_unrelated_event_with_the_expected_event_hash(core):
    canonical_id = normalized()["event_id"]
    other = incoming(core, 43, event_id=canonical_id)
    assert recall(core, verified=False)["event_ids"] == []
    assert core.store.one("SELECT redacted FROM events WHERE event_id=?", (other,))["redacted"] == 0


def test_pending_notice_does_not_assume_existing_private_message_direction(core):
    event_id = incoming(core)
    previous = epoch(core.store)
    result = recall(core, verified=False)
    assert result["state"] == "PENDING_TARGET" and not result["event_ids"]
    assert (
        core.store.one("SELECT redacted FROM events WHERE event_id=?", (event_id,))["redacted"] == 0
    )
    assert epoch(core.store) == previous


def test_confirmed_recall_requires_hash_bound_input_and_matching_source(core):
    event_id = incoming(core)
    result = recall(core)
    assert result["event_ids"] == [event_id]
    assert core.store.one(
        "SELECT body_json,redacted FROM events WHERE event_id=?", (event_id,)
    ) == {
        "body_json": "{}",
        "redacted": 1,
    }
    assert core.store.one("SELECT state,lease_token FROM jobs WHERE event_id=?", (event_id,)) == {
        "state": "CANCELLED",
        "lease_token": None,
    }
    assert is_recalled(core.store, source()["source_event_id"])


def test_source_only_or_wrong_event_id_cannot_confirm_recall(core):
    event_id = incoming(core, event_id="evt_noncanonical_import")
    with pytest.raises(CoreError, match="INPUT_RECALL_TARGET_MISMATCH"):
        recall(core)
    with pytest.raises(CoreError, match="INPUT_RECALL_TARGET_MISMATCH"):
        recall_input(
            core.store, core.ledger, source(), core.clock(), verified=True, event_id=event_id
        )
    assert (
        core.store.one("SELECT redacted FROM events WHERE event_id=?", (event_id,))["redacted"] == 0
    )


def test_recall_preserves_independent_positive_support_and_removes_unsupported_fact(core, tmp_path):
    first, second = incoming(core), incoming(core, 43)
    supported = remember(core, first)
    assert remember(core, second) == supported
    # A separately derived fact has the same sole source, but no independent support.
    core.store.insert(
        "memories",
        memory_id="mem_only_recalled",
        subject="user",
        kind="preference",
        content="仅此来源的细节",
        status="CONFIRMED",
        origin="event_observed",
        confidence=1,
        importance=0.5,
        privacy_scope="private",
        created_at=core.clock(),
        updated_at=core.clock(),
    )
    core.store.insert(
        "memory_evidence",
        memory_id="mem_only_recalled",
        event_id=first,
        polarity=1,
        created_at=core.clock(),
    )
    archive, destination, key = (
        tmp_path / "before.enc",
        tmp_path / "restore.db",
        Fernet.generate_key(),
    )
    backup(core.settings.database, archive, key)
    recall(core)
    restore(archive, destination, key, core.settings.erasure_ledger)
    for store in (core.store,):
        assert store.one("SELECT content,status FROM memories WHERE memory_id=?", (supported,)) == {
            "content": "我喜欢绿茶",
            "status": "CONFIRMED",
        }
    with closing(Store(str(destination))) as restored:
        for store in (core.store, restored):
            assert store.rows(
                "SELECT event_id FROM memory_evidence WHERE memory_id=?", (supported,)
            ) == [{"event_id": second}]
            assert store.one(
                "SELECT content,fact_key,status FROM memories WHERE memory_id='mem_only_recalled'"
            ) == {
                "content": "",
                "fact_key": "",
                "status": "FORGOTTEN",
            }
            assert Memory(store, core.settings, core.ledger).search("仅此来源") == []
            assert (
                store.one("SELECT redacted FROM events WHERE event_id=?", (second,))["redacted"]
                == 0
            )
            before = epoch(store)
            core.ledger.replay(store)
            assert epoch(store) == before


@pytest.mark.parametrize("invalid", ["negative", "derived", "expired", "tombstoned"])
def test_nonindependent_or_ineligible_evidence_does_not_keep_recalled_fact(core, invalid):
    first, second = incoming(core), incoming(core, 43)
    memory_id = remember(core, first)
    assert remember(core, second) == memory_id
    if invalid == "negative":
        core.store.execute("UPDATE memory_evidence SET polarity=-1 WHERE event_id=?", (second,))
    elif invalid == "derived":
        core.store.insert("event_dependencies", event_id=second, source_event_id=first)
    elif invalid == "expired":
        core.store.execute(
            "UPDATE events SET ingested_at=? WHERE event_id=?",
            (after(core.clock(), -86400 * 31), second),
        )
    else:
        core.store.execute(
            "INSERT INTO runtime_meta VALUES (?,?)", ("erased_event:" + second, "old")
        )
    recall(core)
    assert core.store.one(
        "SELECT content,status FROM memories WHERE memory_id=?", (memory_id,)
    ) == {
        "content": "",
        "status": "FORGOTTEN",
    }
    assert not core.memory.search("绿茶")


@pytest.mark.parametrize("legacy", [False, True])
def test_recall_correction_erases_its_derived_copy_without_forgetting_original_family(core, legacy):
    first = incoming(core)
    memory_id = remember(core, first)
    correction = incoming(core, 43, f"/纠正这一条 {memory_id} 我喜欢红茶")
    revised = core.memory.decide(
        memory_id, "correct", core.clock(), "我喜欢红茶", evidence_event_id=correction
    )["memory_id"]
    if legacy:
        core.store.execute("DELETE FROM event_dependencies")
    recall(core, 43)
    assert core.store.one("SELECT content,status FROM memories WHERE memory_id=?", (revised,)) == {
        "content": "",
        "status": "FORGOTTEN",
    }
    assert core.store.one(
        "SELECT content,status FROM memories WHERE memory_id=?", (memory_id,)
    ) == {
        "content": "我喜欢绿茶",
        "status": "SUPERSEDED",
    }
    assert core.store.one("SELECT redacted FROM events WHERE event_id=?", (first,))["redacted"] == 0
    assert not core.store.rows("SELECT * FROM events WHERE source='owner.decision' AND redacted=0")


def test_recall_keeps_independent_correction_and_explicit_forget_remains_strict(core):
    first = incoming(core)
    memory_id = remember(core, first)
    correction = incoming(core, 43, f"/纠正这一条 {memory_id} 我喜欢红茶")
    revised = core.memory.decide(
        memory_id, "correct", core.clock(), "我喜欢红茶", evidence_event_id=correction
    )["memory_id"]
    recall(core)
    assert core.store.one("SELECT content,status FROM memories WHERE memory_id=?", (revised,)) == {
        "content": "我喜欢红茶",
        "status": "CONFIRMED",
    }
    core.memory.decide(revised, "forget", core.clock())
    assert not core.store.rows("SELECT * FROM memories WHERE status<>'FORGOTTEN' OR content<>''")
    assert not core.store.rows("SELECT * FROM events WHERE redacted=0")


def test_fsynced_recall_retries_same_record_after_cleanup_rollback(core, monkeypatch):
    first = incoming(core)
    original = affect.erase_affect

    def fail(store, event_ids):
        original(store, event_ids)
        raise RuntimeError("RECALL_CLEANUP_FAILED")

    monkeypatch.setattr(affect, "erase_affect", fail)
    with pytest.raises(RuntimeError, match="RECALL_CLEANUP_FAILED"):
        recall(core)
    assert len(core.ledger.read()) == 2
    assert not core.store.rows("SELECT * FROM privacy_erasure_ledger")
    assert not is_recalled(core.store, source()["source_event_id"])
    monkeypatch.setattr(affect, "erase_affect", original)
    result = recall(core)
    assert result["duplicate"] and result["event_ids"] == [first]
    assert len(core.ledger.read()) == 2
    before = epoch(core.store)
    core.ledger.replay(core.store)
    assert epoch(core.store) == before


def test_complete_marker_repair_retains_new_unrelated_reply_and_accounting(core):
    first = incoming(core)
    old_conversation = core.store.one(
        "SELECT conversation_id FROM events WHERE event_id=?", (first,)
    )["conversation_id"]
    old_reply = enqueue(core.store, first, old_conversation, "旧回复", core.clock())
    core.store.execute(
        "UPDATE outbox SET state='SENDING',lease_token='send-proof',"
        "attempt_count=1 WHERE message_id=?",
        (old_reply,),
    )
    usage = budget.reserve(core.store, core.settings, None, "chat", [], core.clock())
    budget.settle(core.store, core.settings, usage)
    usage_before = core.store.one("SELECT * FROM model_usage WHERE request_id=?", (usage,))
    recall(core)
    old = core.store.one(
        "SELECT state,lease_token,attempt_count,content_json FROM outbox WHERE message_id=?",
        (old_reply,),
    )
    assert old == {
        "state": "DELIVERY_UNCERTAIN",
        "lease_token": "send-proof",
        "attempt_count": 1,
        "content_json": "{}",
    }
    assert core.store.one("SELECT * FROM model_usage WHERE request_id=?", (usage,)) == usage_before
    second = incoming(core, 43, "独立新问题")
    reply = enqueue(core.store, second, old_conversation, "独立新回复", core.clock())
    expected = core.store.one("SELECT * FROM outbox WHERE message_id=?", (reply,))
    core.store.execute(
        "DELETE FROM runtime_meta WHERE key=?", (marker_key(source()["source_event_id"]),)
    )
    core.ledger.replay(core.store)
    assert is_recalled(core.store, source()["source_event_id"])
    assert core.store.one("SELECT * FROM outbox WHERE message_id=?", (reply,)) == expected
    assert core.store.one("SELECT * FROM model_usage WHERE request_id=?", (usage,)) == usage_before
    core.store.execute("DELETE FROM runtime_meta WHERE key='derived_erasure_contract'")
    core.ledger.replay(core.store)
    assert core.store.one("SELECT * FROM outbox WHERE message_id=?", (reply,)) == expected


def test_tracked_reply_sources_are_invalidated_without_erasing_independent_owner_input(core):
    first, second, third = incoming(core), incoming(core, 43), incoming(core, 44)
    fourth = incoming(core, 45)
    jobs = {row["event_id"]: row["job_id"] for row in core.store.rows("SELECT * FROM jobs")}
    core.store.execute("UPDATE jobs SET context_tracked=1")
    for origin, dependency in (
        (first, first), (second, first), (second, second), (third, third),
        (fourth, second), (fourth, fourth),
    ):
        core.store.execute(
            "INSERT OR IGNORE INTO job_sources VALUES (?,?)", (jobs[origin], dependency)
        )
    conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id=?", (first,))[
        "conversation_id"
    ]
    dependent = enqueue(core.store, second, conversation, "引用了第一条输入", core.clock())
    independent = enqueue(core.store, third, conversation, "仅基于第三条输入", core.clock())
    transitive = enqueue(core.store, fourth, conversation, "引用了第二条回复", core.clock())
    expected = core.store.one("SELECT * FROM outbox WHERE message_id=?", (independent,))
    recall(core)
    assert core.store.one(
        "SELECT content_json,state FROM outbox WHERE message_id=?", (dependent,)
    ) == {
        "content_json": "{}",
        "state": "CANCELLED",
    }
    assert core.store.one("SELECT * FROM outbox WHERE message_id=?", (independent,)) == expected
    assert core.store.one("SELECT content_json FROM outbox WHERE message_id=?", (transitive,)) == {
        "content_json": "{}",
    }
    assert (
        core.store.one("SELECT redacted FROM events WHERE event_id=?", (second,))["redacted"] == 0
    )
    assert core.store.one("SELECT redacted FROM events WHERE event_id=?", (third,))["redacted"] == 0
    assert core.store.one("SELECT redacted FROM events WHERE event_id=?", (fourth,)) == {
        "redacted": 0,
    }


def test_malformed_recall_ledger_source_is_rejected_before_replay(core):
    recall(core)
    lines = core.ledger.read()
    lines[1]["recall_source"]["owner_qq_id"] = "99999"
    core.ledger.path.write_text("\n".join(encode(line) for line in lines) + "\n", encoding="utf-8")
    with pytest.raises(CoreError, match="ERASURE_LEDGER_INVALID"):
        core.ledger.replay(core.store)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_message_id": True},
        {"provider_message_id": "0"},
        {"provider_message_id": "01"},
        {"protocol_epoch": "short"},
        {"owner_qq_id": "010001"},
        {"owner_qq_id": "10002"},
        {"platform_session_key": "onebot-direct:GroupMessage:10001"},
    ],
)
def test_source_identity_requires_canonical_owner_private_namespace(changes):
    with pytest.raises(ValueError):
        source(**changes)
