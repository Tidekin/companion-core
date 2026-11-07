"""Natural raw retention preserves independent support and explicit erasure."""

from contextlib import closing

import pytest
from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.contracts import Candidate
from companion.memory import Memory
from companion.privacy import epoch
from companion.store import Store
from tests.conftest import ingest
from tests.support.confirmed_memory import remember


def renewed_fact(core, *, semantic=False):
    core.settings.raw_event_retention_days = 3
    mid = remember(core, 1, "我喜欢绿茶", confirmed=True)
    core.clock.advance(2 * 86400)
    if semantic:
        text = "绿茶是我爱喝的茶"
        source = ingest(core, 2, text)
        with core.store.transaction():
            assert (
                core.memory.add(
                    Candidate(
                        subject="user",
                        kind="preference",
                        fact_key="饮料",
                        content=text,
                        evidence_event_id=source["event_id"],
                        evidence_quote=text,
                    ),
                    core.clock(),
                    confirmed=True,
                    resolution={"same": mid, "conflicts": []},
                )
                == mid
            )
    else:
        assert remember(core, 2, "我喜欢绿茶", confirmed=True) == mid
    core.store.execute("UPDATE events SET memory_processed=1")
    core.clock.advance(2 * 86400)
    return mid


def assert_supported(store, mid):
    assert store.one(
        "SELECT status,content,confidence FROM memories WHERE memory_id=?", (mid,)
    ) == {
        "status": "CONFIRMED",
        "content": "我喜欢绿茶",
        "confidence": 1.0,
    }
    assert store.rows("SELECT event_id FROM memory_evidence WHERE memory_id=?", (mid,)) == [
        {"event_id": "evt_test_2"},
    ]
    assert store.one("SELECT body_json,redacted FROM events WHERE event_id='evt_test_1'") == {
        "body_json": "{}",
        "redacted": 1,
    }
    assert store.one(
        "SELECT redacted,memory_processed FROM events WHERE event_id='evt_test_2'"
    ) == {
        "redacted": 0,
        "memory_processed": 1,
    }


@pytest.mark.parametrize("semantic", [False, True])
def test_old_support_expires_without_erasing_fresh_support_or_reviving_raw_backup(
    core,
    tmp_path,
    semantic,
):
    mid = renewed_fact(core, semantic=semantic)
    archive, destination, key = (
        tmp_path / "before.enc",
        tmp_path / "restored.db",
        Fernet.generate_key(),
    )
    backup(core.settings.database, archive, key)
    assert core.expire_raw_events() == 1
    assert_supported(core.store, mid)
    assert core.ledger.read()[-1]["target_type"] == "raw_retention"
    restore(archive, destination, key, core.settings.erasure_ledger)
    with closing(Store(str(destination))) as restored:
        assert_supported(restored, mid)
        for store in (core.store, restored):
            before = epoch(store)
            core.ledger.replay(store)
            core.ledger.replay(store)
            assert epoch(store) == before
            store.execute("DELETE FROM runtime_meta WHERE key='derived_erasure_contract'")
            core.ledger.replay(store)
            assert_supported(store, mid)
            assert mid in {
                row["memory_id"]
                for row in Memory(
                    store,
                    core.settings,
                    core.ledger,
                ).search("绿茶")
            }
    core.clock.advance(2 * 86400)
    assert core.expire_raw_events() == 1
    assert core.expire_raw_events() == 0
    assert core.store.one(
        "SELECT status,confidence,needs_review FROM memories WHERE memory_id=?",
        (mid,),
    ) == {"status": "HYPOTHESIS", "confidence": 0.0, "needs_review": 1}
    assert core.memory.search("绿茶") == []
    assert not core.store.rows("SELECT * FROM memory_vectors")
    assert not core.store.rows("SELECT * FROM memory_evidence")


def test_explicit_forget_after_retention_still_erases_fact_family_and_old_backup(core, tmp_path):
    mid = renewed_fact(core)
    assert core.expire_raw_events() == 1
    corrected = core.memory.decide(mid, "correct", core.clock(), "我喜欢红茶")["memory_id"]
    archive, destination, key = (
        tmp_path / "before.enc",
        tmp_path / "restored.db",
        Fernet.generate_key(),
    )
    backup(core.settings.database, archive, key)
    core.memory.decide(corrected, "forget", core.clock())
    restore(archive, destination, key, core.settings.erasure_ledger)
    with closing(Store(str(destination))) as restored:
        for store in (core.store, restored):
            assert store.one("SELECT COUNT(*) n FROM memories WHERE status='FORGOTTEN'")["n"] == 2
            assert not store.rows("SELECT * FROM memories WHERE content<>''")
            assert not store.rows("SELECT * FROM memory_evidence")
            assert not store.rows("SELECT * FROM memory_revisions")
            assert not store.rows("SELECT * FROM events WHERE redacted=0")
            assert not store.rows("SELECT * FROM memories_fts")


@pytest.mark.parametrize("legacy", [False, True])
def test_correction_is_new_support_but_its_derived_copy_is_not_independent(core, legacy):
    core.settings.raw_event_retention_days = 3
    mid = remember(core, 1, "我喜欢绿茶", confirmed=True)
    core.clock.advance(2 * 86400)
    correction = ingest(core, 2, f"/纠正这一条 {mid} 我喜欢红茶")
    revised = core.memory.decide(
        mid, "correct", core.clock(), "我喜欢红茶", evidence_event_id=correction["event_id"]
    )["memory_id"]
    if legacy:
        core.store.execute("DELETE FROM event_dependencies")
    core.clock.advance(2 * 86400)
    assert core.expire_raw_events() == 1
    assert core.store.one("SELECT status FROM memories WHERE memory_id=?", (revised,)) == {
        "status": "CONFIRMED",
    }
    # A legacy derived event can be newer than its original command. Its source
    # link still prevents counting the copy as fresh independent confirmation.
    core.store.execute(
        "UPDATE events SET ingested_at=? WHERE source='owner.decision'", (core.clock(),)
    )
    core.clock.advance(2 * 86400)
    assert core.expire_raw_events() == 1
    assert core.store.one("SELECT redacted FROM events WHERE source='owner.decision'") == {
        "redacted": 1,
    }
    assert core.store.one(
        "SELECT status,confidence FROM memories WHERE memory_id=?", (revised,)
    ) == {
        "status": "HYPOTHESIS",
        "confidence": 0.0,
    }
    assert core.memory.search("红茶") == []


@pytest.mark.parametrize("dependent", [False, True])
def test_negative_or_derived_evidence_cannot_keep_expired_fact_active(core, dependent):
    mid = renewed_fact(core)
    if dependent:
        core.store.insert("event_dependencies", event_id="evt_test_2", source_event_id="evt_test_1")
    else:
        core.store.execute("UPDATE memory_evidence SET polarity=-1 WHERE event_id='evt_test_2'")
    assert core.expire_raw_events() == 1
    assert core.store.one("SELECT status,confidence FROM memories WHERE memory_id=?", (mid,)) == {
        "status": "HYPOTHESIS",
        "confidence": 0.0,
    }
    assert core.memory.search("绿茶") == []
