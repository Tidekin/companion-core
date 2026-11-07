"""Read-time expiry uses the same legacy correction provenance as Ledger cleanup."""

import pytest
from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.core import Core
from companion.memory_context import MemoryContext, MemorySources
from tests.conftest import ingest
from tests.support.confirmed_memory import remember


def delayed_correction(core, support):
    core.settings.raw_event_retention_days = 3
    old = remember(core, 1, "常用饮料是绿茶", confirmed=True)
    core.clock.advance(2 * 86400)
    command = ingest(core, 2, f"/纠正这一条 {old} 常用饮料是红茶")
    core.clock.advance(2 * 86400)
    revised = core.memory.decide(old, "correct", core.clock(), "常用饮料是红茶",
                                 evidence_event_id=command["event_id"])["memory_id"]
    copied = core.store.one("SELECT event_id FROM events WHERE source='owner.decision'")["event_id"]
    # The supported legacy backup shape: only the explicit dependency is absent.
    core.store.execute("DELETE FROM event_dependencies")
    if support != "none":
        assert remember(core, 3, "常用饮料是红茶", confirmed=True) == revised
        if support == "dependent":
            core.store.insert("event_dependencies", event_id="evt_test_3", source_event_id=copied)
    return revised, command["event_id"], copied


def assert_read_contract(core, revised, original, copied, support):
    view = MemoryContext(core.memory)
    sources = MemorySources(core.memory, core.clock())
    expected = [revised] if support == "independent" else []
    assert sources.event(original) is None
    assert sources.event(copied) is None
    if support == "dependent":
        assert sources.event("evt_test_3") is None
    # Search has its own evidence path, separate from the two higher-level views.
    for query in ("*", "红茶"):
        rows = core.memory.search(query, now=core.clock())
        assert [row["memory_id"] for row in rows] == expected
        if rows:
            assert rows[0]["evidence_ids"] == ["evt_test_3"]
    recall = view.recall("红茶", core.clock())
    assert recall["state"] == ("FOUND" if expected else "NO_RECORD")
    assert [row["memory_id"] for row in recall["facts"]] == expected
    assert not recall["conflicts"]
    history = view.history(revised, core.clock())["revisions"]
    assert [row["memory_id"] for row in history] == expected
    for rows in (recall["facts"], history):
        if rows:
            assert [source["event_id"] for source in rows[0]["evidence"]] == ["evt_test_3"]
    plan = view.invalidation_plan([original], core.clock())
    if expected:
        assert plan["retain"] == [{"memory_id": revised, "evidence_ids": ["evt_test_3"]}]
        assert plan["withdraw"] == []
    else:
        assert plan["retain"] == []
        assert plan["withdraw"] == [revised]
    expected_closure = {original, copied}
    if support == "dependent":
        expected_closure.add("evt_test_3")
    assert set(plan["source_event_ids"]) == expected_closure


@pytest.mark.parametrize("support", ["none", "independent", "dependent"])
def test_legacy_correction_expires_before_cleanup_and_after_backup_restore(core, tmp_path, support):
    revised, original, copied = delayed_correction(core, support)
    view = MemoryContext(core.memory)
    assert view.recall("红茶", core.clock())["state"] == "FOUND"
    archive, key = tmp_path / "legacy-before-expiry.enc", Fernet.generate_key()
    backup(core.settings.database, archive, key)
    core.clock.advance(2 * 86400)
    ledger = core.ledger.path.read_bytes()
    state = core.store.rows("SELECT * FROM events ORDER BY event_id")
    assert_read_contract(core, revised, original, copied, support)
    assert not core.store.rows("SELECT * FROM privacy_erasure_ledger")
    assert core.ledger.path.read_bytes() == ledger
    assert core.store.rows("SELECT * FROM events ORDER BY event_id") == state
    # Restore without any cleanup record: startup replay alone cannot implement read-time expiry.
    destination = tmp_path / "restored-before-cleanup.db"
    restore(archive, destination, key, core.settings.erasure_ledger)
    reopened = Core(core.settings.model_copy(update={"database": str(destination)}),
                    core.model, core.clock)
    try:
        assert_read_contract(reopened, revised, original, copied, support)
        assert not reopened.store.rows("SELECT * FROM privacy_erasure_ledger")
        assert not any(row["redacted"] for row in reopened.store.rows(
            "SELECT redacted FROM events",
        ))
    finally:
        reopened.close()
    # Then exercise actual Ledger cleanup and replay against the exact same old backup.
    assert core.expire_raw_events() == 2
    destination = tmp_path / "restored-after-cleanup.db"
    restore(archive, destination, key, core.settings.erasure_ledger)
    reopened = Core(core.settings.model_copy(update={"database": str(destination)}),
                    core.model, core.clock)
    try:
        for instance in (core, reopened):
            result = MemoryContext(instance.memory).recall("红茶", core.clock())
            assert result["state"] == ("FOUND" if support == "independent" else "NO_RECORD")
            assert [row["memory_id"] for row in result["facts"]] == (
                [revised] if support == "independent" else []
            )
            assert instance.store.one("SELECT redacted FROM events WHERE event_id=?",
                                      (copied,)) == {"redacted": 1}
    finally:
        reopened.close()


def test_native_owner_decision_is_not_inferred_to_depend_on_old_fact(core):
    core.settings.raw_event_retention_days = 3
    old = remember(core, 1, "常用饮料是绿茶", confirmed=True)
    core.clock.advance(4 * 86400)
    revised = core.memory.decide(old, "correct", core.clock(), "常用饮料是红茶")["memory_id"]
    # This authenticated native decision has no raw QQ correction source to expire with.
    assert not core.store.rows("SELECT * FROM event_dependencies")
    result = MemoryContext(core.memory).recall("红茶", core.clock())
    assert [row["memory_id"] for row in result["facts"]] == [revised]
    assert result["facts"][0]["evidence"][0]["source"] == "owner.decision"
