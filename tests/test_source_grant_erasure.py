from contextlib import closing

import pytest
from cryptography.fernet import Fernet

import companion.privacy as privacy
from companion.backup import backup, restore
from companion.config import ObservationAdapterConfig
from companion.contracts import CoreError
from companion.store import Store
from tests.conftest import event
from tests.support.source_grants import accepted, derived_records, prepare
from tests.test_observations import observation


def corrected_source_memory(core):
    snapshot = prepare(core)
    root = accepted(core, snapshot)
    derived_records(core, root)
    core.settings.memory_enabled = True
    memory_ids = ["mem_grant_fixture"]
    corrections = []
    for number in range(2):
        corrected = core.memory.decide(
            memory_ids[-1], "correct", core.clock(), f"Private corrected source fact {number}"
        )["memory_id"]
        memory_ids.append(corrected)
        correction = core.store.one(
            "SELECT event_id FROM memory_evidence WHERE memory_id=?", (corrected,)
        )["event_id"]
        corrections.append(correction)
        core.store.insert(
            "memory_vectors",
            memory_id=corrected,
            model_version="fixture",
            content_hash="b" * 64,
            vector=b"\0" * 16,
        )
    core.store.execute("UPDATE diary_evidence SET event_id=?", (corrections[-1],))
    core.settings.observation_adapters["derived"] = ObservationAdapterConfig(
        source="simulation", token="derived-test-" * 4
    )
    derived = observation(core, 899, source_event_ids=[corrections[-1]])
    core.observations.append("derived", derived)
    unrelated = event(core, 900, text="Unrelated owner history")
    core.ingest(unrelated, "unrelated-owner-history")
    now = core.clock()
    core.store.insert(
        "memories",
        memory_id="mem_unrelated_owner",
        subject="self",
        kind="fact",
        content="Unrelated owner memory",
        status="CONFIRMED",
        origin="event_observed",
        confidence=1,
        importance=0.5,
        privacy_scope="private",
        created_at=now,
        updated_at=now,
    )
    core.store.insert(
        "memory_evidence",
        memory_id="mem_unrelated_owner",
        event_id=unrelated.event_id,
        polarity=1,
        created_at=now,
    )
    return snapshot, [root, *corrections, derived.event_id], memory_ids, unrelated.event_id


def assert_erased(store, event_ids, memory_ids, unrelated):
    for memory_id in memory_ids:
        assert store.one("SELECT content,status FROM memories WHERE memory_id=?", (memory_id,)) == {
            "content": "",
            "status": "FORGOTTEN",
        }
        assert not store.rows("SELECT * FROM memory_vectors WHERE memory_id=?", (memory_id,))
    for event_id in event_ids:
        assert store.one("SELECT body_json,redacted FROM events WHERE event_id=?", (event_id,)) == {
            "body_json": "{}",
            "redacted": 1,
        }
    assert store.one("SELECT body_md,status FROM diary_entries") == {
        "body_md": "",
        "status": "REDACTED",
    }
    assert not store.rows("SELECT * FROM diary_evidence")
    assert store.one("SELECT redacted FROM events WHERE event_id=?", (unrelated,)) == {
        "redacted": 0
    }
    assert store.one(
        "SELECT content,status FROM memories WHERE memory_id='mem_unrelated_owner'"
    ) == {
        "content": "Unrelated owner memory",
        "status": "CONFIRMED",
    }
    assert not store.rows("PRAGMA foreign_key_check")


def test_grant_forget_erases_supersession_closure_and_restored_backup(core, tmp_path):
    snapshot, event_ids, memory_ids, unrelated = corrected_source_memory(core)
    key = Fernet.generate_key()
    archive, destination = tmp_path / "before-forget.enc", tmp_path / "restored.db"
    backup(core.settings.database, archive, key)
    core.observations.grants.forget(snapshot.grant_id, core.ledger)
    assert_erased(core.store, event_ids, memory_ids, unrelated)
    record = core.ledger.read()[-1]
    assert set(record["memory_ids"]) == set(memory_ids)
    assert set(event_ids) <= set(record["event_ids"])
    restore(archive, destination, key, core.settings.erasure_ledger)
    with closing(Store(str(destination))) as restored:
        assert_erased(restored, event_ids, memory_ids, unrelated)


def test_complete_grant_projection_reapplies_stale_recorded_successor(core):
    snapshot, event_ids, memory_ids, unrelated = corrected_source_memory(core)
    core.observations.grants.forget(snapshot.grant_id, core.ledger)
    with core.store.transaction():
        # Keep the root redacted, COMPLETE projection and grant tombstone intact.
        core.store.execute(
            "UPDATE memories SET content='STALE_PRIVATE_SUCCESSOR',status='CONFIRMED' "
            "WHERE memory_id=?",
            (memory_ids[-1],),
        )
        core.store.execute(
            "UPDATE events SET body_json=?,redacted=0 WHERE event_id=?",
            ('{"text":"STALE_PRIVATE_CORRECTION"}', event_ids[-2]),
        )
    core.ledger.replay(core.store)
    assert_erased(core.store, event_ids, memory_ids, unrelated)
    assert len(core.ledger.read()) == 2


@pytest.mark.parametrize("recovery", ["startup_replay", "encrypted_restore"])
def test_legacy_complete_with_lost_provenance_requires_isolated_review(
    core, tmp_path, monkeypatch, recovery
):
    snapshot, event_ids, memory_ids, unrelated = corrected_source_memory(core)
    # Exact pre-fix behavior: persist grant roots only, erase the directly evidenced
    # predecessor, and lose its memory_evidence before marking the projection COMPLETE.
    with monkeypatch.context() as old_version:
        old_version.setattr(
            privacy,
            "source_grant_closure",
            lambda store, events, memories=(): (set(events), set(memories)),
        )
        core.observations.grants.forget(snapshot.grant_id, core.ledger)
    record = core.ledger.read()[-1]
    assert record["memory_ids"] == [] and record["event_ids"] == [event_ids[0]]
    assert core.store.one("SELECT status FROM memories WHERE memory_id=?", (memory_ids[0],)) == {
        "status": "FORGOTTEN"
    }
    assert not core.store.rows("SELECT * FROM memory_evidence WHERE memory_id=?", (memory_ids[0],))
    assert core.store.one("SELECT status FROM memories WHERE memory_id=?", (memory_ids[-1],)) == {
        "status": "CONFIRMED"
    }
    ledger_bytes = core.ledger.path.read_bytes()
    before = core.store.rows("SELECT event_id,body_json,redacted FROM events ORDER BY event_id")
    before_memories = core.store.rows("SELECT * FROM memories ORDER BY memory_id")
    if recovery == "startup_replay":
        # Core startup calls this same replay path before processing any work.
        with pytest.raises(CoreError, match="SOURCE_GRANT_LEGACY_ERASURE_REVIEW_REQUIRED"):
            core.ledger.replay(core.store)
    else:
        key = Fernet.generate_key()
        archive, destination = tmp_path / "legacy-complete.enc", tmp_path / "restored.db"
        backup(core.settings.database, archive, key)
        with pytest.raises(CoreError, match="SOURCE_GRANT_LEGACY_ERASURE_REVIEW_REQUIRED"):
            restore(archive, destination, key, core.settings.erasure_ledger)
        assert not destination.exists()
    assert core.store.rows("SELECT * FROM memories ORDER BY memory_id") == before_memories
    assert (
        core.store.rows("SELECT event_id,body_json,redacted FROM events ORDER BY event_id")
        == before
    )
    assert core.store.one("SELECT redacted FROM events WHERE event_id=?", (unrelated,)) == {
        "redacted": 0
    }
    assert core.ledger.path.read_bytes() == ledger_bytes
