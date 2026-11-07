"""Observable rollback/replay guarantees at the module cleanup boundary."""

from contextlib import closing

import pytest
from cryptography.fernet import Fernet

import companion.affect as affect
from companion import budget
from companion.backup import backup, restore
from companion.privacy import epoch
from companion.store import Store
from companion.transport import enqueue
from tests.conftest import ingest
from tests.support.observation_capture import enable_observations, observation


def test_replay_contract_discovery_does_not_hold_database_write_lock(core, monkeypatch):
    from companion import erasure

    original = erasure.contract_signature
    ledger = core.ledger.path.read_bytes()
    with closing(Store(core.settings.database)) as other:
        other.execute("PRAGMA busy_timeout=0")

        def discover():
            signature = original()
            with other.transaction():
                other.execute("INSERT INTO runtime_meta VALUES ('synthetic-concurrent-boot','1')")
            return signature

        monkeypatch.setattr(erasure, "contract_signature", discover)
        core.ledger.replay(core.store)
    assert core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                          (erasure.CONTRACT_KEY,))["value"] == original()
    assert core.ledger.path.read_bytes() == ledger


def legacy_complete_with_stale_affect(core):
    core.settings.affect_enabled = True
    ingest(core, 1, "简短一点")
    evidence = core.store.one("SELECT * FROM affect_evidence")
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    assert core.store.one("SELECT state FROM privacy_erasure_ledger")["state"] == "COMPLETE"
    # A synthetic old implementation marked COMPLETE while omitting this module.
    # The event remains tombstoned; the stale projection still names its source.
    core.store.insert("affect_evidence", **evidence)
    core.store.execute("DELETE FROM runtime_meta WHERE key='derived_erasure_contract'")
    ingest(core, 2, "独立的新对话不能被旧记录重放清空。")
    event = core.store.one("SELECT * FROM events WHERE event_id='evt_test_2'")
    message = enqueue(
        core.store, event["event_id"], event["conversation_id"], "独立的新回复", core.clock()
    )
    core.store.execute("UPDATE outbox SET state='SENDING' WHERE message_id=%s", (message,))
    usage = budget.reserve(core.store, core.settings, None, "chat", [], core.clock())
    budget.settle(core.store, core.settings, usage)
    return message, core.store.one('SELECT * FROM model_usage WHERE request_id=%s', (usage,))


def test_old_complete_backup_repairs_sourced_projection_without_reclearing_new_replies(
    core, tmp_path
):
    message, usage = legacy_complete_with_stale_affect(core)
    archive, destination = tmp_path / "legacy.enc", tmp_path / "restored.sqlite3"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    ledger_bytes = core.ledger.path.read_bytes()
    restore(archive, destination, key, core.settings.erasure_ledger)
    with closing(Store(str(destination))) as store:
        assert store.rows("SELECT * FROM affect_evidence") == []
        assert store.one("SELECT redacted FROM events WHERE event_id='evt_test_1'")["redacted"] == 1
        assert store.one("SELECT redacted FROM events WHERE event_id='evt_test_2'")["redacted"] == 0
        reply = store.one("SELECT * FROM outbox WHERE message_id=?", (message,))
        assert "独立的新回复" in reply["content_json"]
        assert reply["state"] == "DELIVERY_UNCERTAIN"
        assert (
            store.one("SELECT * FROM model_usage WHERE request_id=?", (usage["request_id"],))
            == usage
        )
        completed_epoch = epoch(store)
        core.ledger.replay(store)
        assert epoch(store) == completed_epoch
        assert store.one("SELECT * FROM outbox WHERE message_id=?", (message,)) == reply
    assert core.ledger.path.read_bytes() == ledger_bytes


def test_handler_failure_rolls_back_db_but_fsynced_record_replays(core, monkeypatch):
    core.settings.affect_enabled = True
    ingest(core, 1, "简短一点")
    expected_epoch = epoch(core.store)
    original = affect.erase_affect

    def fail_after_cleanup(store, event_ids):
        original(store, event_ids)
        raise RuntimeError("SYNTHETIC_HANDLER_FAILURE")

    monkeypatch.setattr(affect, "erase_affect", fail_after_cleanup)
    with pytest.raises(RuntimeError, match="SYNTHETIC_HANDLER_FAILURE"):
        with core.store.transaction():
            core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    assert len(core.ledger.read()) == 2
    assert core.store.rows("SELECT * FROM privacy_erasure_ledger") == []
    assert (
        core.store.one("SELECT redacted FROM events WHERE event_id='evt_test_1'")["redacted"] == 0
    )
    assert core.store.rows("SELECT * FROM affect_evidence")
    assert epoch(core.store) == expected_epoch
    monkeypatch.setattr(affect, "erase_affect", original)
    core.ledger.replay(core.store)
    assert core.store.rows("SELECT * FROM affect_evidence") == []
    assert (
        core.store.one("SELECT redacted FROM events WHERE event_id='evt_test_1'")["redacted"] == 1
    )


def test_old_complete_repairs_derived_ids_recorded_before_dependency_links_were_erased(
    core, tmp_path
):
    ingest(core, 1, "合成来源内容")
    enable_observations(core)
    item = observation(core, source_event_ids=["evt_test_1"])
    core.observations.append("reader", item)
    saved_event = core.store.one('SELECT * FROM events WHERE event_id=%s', (item.event_id,))
    saved_projection = core.store.one(
        'SELECT * FROM observations WHERE event_id=%s', (item.event_id,)
    )
    core.store.insert(
        "memories",
        memory_id="mem_derived_fixture",
        subject="self",
        kind="fact",
        content="合成派生记忆",
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
        memory_id="mem_derived_fixture",
        event_id=item.event_id,
        polarity=1,
        created_at=core.clock(),
    )
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    record = core.ledger.read()[1]
    assert item.event_id not in record["event_ids"]
    assert core.store.rows("SELECT * FROM event_dependencies") == []
    assert (
        core.store.one(
            'SELECT value FROM runtime_meta WHERE key=%s', ("erased_event:" + item.event_id,)
        )["value"]
        == record["erasure_id"]
    )
    # Explicit attribution survived, even though an old completed snapshot kept
    # a derived module's body/projection and its dependency links are already gone.
    core.store.execute(
        'UPDATE events SET body_json=%s,redacted=0 WHERE event_id=%s',
        (saved_event["body_json"], item.event_id),
    )
    core.store.execute(
        (
            'UPDATE observations SET '
            'source_sha256=%s,canonical_url=%s,evidence_json=%s,redacted=0 '
            'WHERE event_id=%s'
        ),
        (
            saved_projection["source_sha256"],
            saved_projection["canonical_url"],
            saved_projection["evidence_json"],
            item.event_id,
        ),
    )
    core.store.execute("DELETE FROM runtime_meta WHERE key='derived_erasure_contract'")
    core.store.execute(
        "UPDATE memories SET content='合成派生记忆',status='CONFIRMED',confidence=1 "
        "WHERE memory_id='mem_derived_fixture'"
    )
    core.store.insert(
        "memory_evidence",
        memory_id="mem_derived_fixture",
        event_id=item.event_id,
        polarity=1,
        created_at=core.clock(),
    )
    core.store.insert(
        "memory_vectors",
        memory_id="mem_derived_fixture",
        model_version="fixture",
        content_hash="a" * 64,
        vector=b"\0" * 16,
    )
    archive, destination = tmp_path / "derived.enc", tmp_path / "derived-restored.sqlite3"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    restore(archive, destination, key, core.settings.erasure_ledger)
    with closing(Store(str(destination))) as store:
        assert store.one(
            "SELECT body_json,redacted FROM events WHERE event_id=?", (item.event_id,)
        ) == {"body_json": "{}", "redacted": 1}
        assert store.one(
            "SELECT source_sha256,canonical_url,redacted FROM observations WHERE event_id=?",
            (item.event_id,),
        ) == {"source_sha256": "", "canonical_url": "", "redacted": 1}
        assert store.one(
            "SELECT content,status FROM memories WHERE memory_id='mem_derived_fixture'"
        ) == {"content": "", "status": "FORGOTTEN"}
        assert store.rows("SELECT * FROM memory_vectors") == []


def test_old_complete_handler_failure_prevents_restored_file_publication(
    core, monkeypatch, tmp_path
):
    legacy_complete_with_stale_affect(core)
    archive, destination = tmp_path / "legacy-failure.enc", tmp_path / "not-published.sqlite3"
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    original = affect.erase_affect

    def failure(store, event_ids):
        original(store, event_ids)
        raise RuntimeError("SYNTHETIC_REPLAY_HANDLER_FAILURE")

    monkeypatch.setattr(affect, "erase_affect", failure)
    with pytest.raises(RuntimeError, match="SYNTHETIC_REPLAY_HANDLER_FAILURE"):
        restore(archive, destination, key, core.settings.erasure_ledger)
    assert not destination.exists()
    assert core.store.rows("SELECT * FROM affect_evidence")
