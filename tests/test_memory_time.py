"""Temporal filters preserve source custody and distinguish plans from message dates."""

import json

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from companion.backup import backup, restore
from companion.contracts import Candidate, CoreError, EventTime, TimeQuery
from companion.core import Core
from tests.conftest import ingest, run
from tests.support.confirmed_memory import remember


def scope(basis, start="2026-01-01T00:00:00+08:00", end="2026-01-02T00:00:00+08:00"):
    return TimeQuery(basis=basis, start=start, end=end)


def recall(core, query):
    return run(core.memory_context.recall_async("*", core.clock(), temporal=query,
                                               clock=core.clock))


def test_event_interval_and_source_message_time_are_independent(core):
    ingest(core, 1, "我计划在2026年1月1日去南门取钥匙")
    value = EventTime(start="2026-01-01T00:00:00+08:00", end="2026-01-02T00:00:00+08:00",
                      precision="day", assertion="planned", quote="2026年1月1日")
    with core.store.transaction(control_write=True):
        mid = core.memory.add(Candidate(subject="user", kind="episode", fact_key="key-trip",
            content="主人计划在2026年1月1日取钥匙", evidence_event_id="evt_test_1",
            evidence_quote="我计划在2026年1月1日去南门取钥匙", event_time=value), core.clock())
    remember(core, 2, "另一个没有明确发生时间的取件计划")
    found = recall(core, scope("occurred"))
    assert [f["memory_id"] for f in found["facts"]] == [mid]
    assert found["facts"][0]["event_time"]["assertion"] == "planned"
    assert recall(core, scope("mentioned"))["state"] == "NO_RECORD"
    # The same instant in UTC selects the same day; the upper edge is exclusive.
    assert recall(core, scope("occurred", "2025-12-31T16:00:00Z",
                             "2026-01-01T16:00:00Z"))["facts"] == found["facts"]
    assert recall(core, scope("occurred", "2026-01-02T00:00:00+08:00",
                             "2026-01-03T00:00:00+08:00"))["state"] == "NO_RECORD"
    core.memory.decide(mid, "forget", core.clock())
    assert recall(core, scope("occurred"))["state"] == "NO_RECORD"
    assert core.store.one("SELECT event_time_json FROM memories WHERE memory_id=%s",
                          (mid,))["event_time_json"] is None


def test_time_quote_must_be_an_exact_source_substring(core):
    ingest(core, 1, "明年再决定取件日期")
    value = EventTime(start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z",
                      precision="day", assertion="uncertain", quote="2026年1月1日")
    with core.store.transaction(control_write=True), pytest.raises(CoreError,
                                                                   match="UNSUPPORTED_MEMORY_TIME"):
        core.memory.add(Candidate(subject="user", kind="episode", fact_key="date",
            content="日期待定", evidence_event_id="evt_test_1", evidence_quote="取件日期",
            event_time=value), core.clock())


def test_known_snapshot_returns_previous_value_but_erasure_still_applies(core):
    mid = remember(core, 1, "我喜欢红茶", fact_key="tea", confirmed=True)
    before = core.clock()
    core.clock.advance(60)
    core.memory.decide(mid, "correct", core.clock(), "我喜欢绿茶")
    old = recall(core, TimeQuery(basis="known", as_of=before))
    assert [f["content"] for f in old["facts"]] == ["我喜欢红茶"]
    assert old["facts"][0]["historical"] and "valid_to" not in old["facts"][0]
    assert recall(core, None)["facts"][0]["content"] == "我喜欢绿茶"
    core.memory.decide(mid, "forget", core.clock())
    assert recall(core, TimeQuery(basis="known", as_of=before))["state"] == "NO_RECORD"
    with pytest.raises(CoreError, match="FUTURE_MEMORY_SNAPSHOT"):
        recall(core, TimeQuery(basis="known", as_of="2099-01-01T00:00:00Z"))


def test_known_snapshot_does_not_display_later_added_evidence(core):
    mid = remember(core, 1, "主人喜欢红茶", confirmed=True)
    before = core.clock()
    core.clock.advance(60)
    assert remember(core, 2, "主人喜欢红茶", confirmed=True) == mid
    old = recall(core, TimeQuery(basis="known", as_of=before))
    assert old["facts"][0]["evidence_ids"] == ["evt_test_1"]
    # The private proof still pins all current support and detects later withdrawal.
    assert old["rows"][0]["evidence_ids"] == ["evt_test_1", "evt_test_2"]


@pytest.mark.parametrize("values", [
    {"basis": "known"}, {"basis": "mentioned", "start": "2026-01-01", "end": "2026-01-02"},
    {"basis": "occurred", "start": "2026-01-02T00:00:00Z", "end": "2026-01-01T00:00:00Z"},
])
def test_ambiguous_or_empty_time_bounds_are_rejected(values):
    with pytest.raises(ValidationError):
        TimeQuery(**values)


def test_nullable_metadata_migration_preserves_existing_fact(core):
    mid = remember(core, 1, "迁移后的普通事实")
    row = core.store.one("SELECT event_time_json FROM memories WHERE memory_id=%s", (mid,))
    assert row == {"event_time_json": None}
    assert json.loads(core.store.one("SELECT body_json FROM events WHERE event_id='evt_test_1'")
                      ["body_json"])["text"] == "迁移后的普通事实"


def test_erased_time_source_stays_erased_after_old_backup_restore(core, empty_database, tmp_path):
    text = "我计划在2026年1月1日取钥匙"
    ingest(core, 1, text)
    with core.store.transaction(control_write=True):
        mid = core.memory.add(Candidate(subject="user", kind="fact", fact_key="key-date",
            content=text, evidence_event_id="evt_test_1", evidence_quote=text,
            event_time=EventTime(start="2026-01-01T00:00:00+08:00",
                end="2026-01-02T00:00:00+08:00", precision="day", assertion="planned",
                quote="2026年1月1日")), core.clock())
    assert remember(core, 2, text, fact_key="key-date") == mid
    key, archive = Fernet.generate_key(), tmp_path / "before.enc"
    backup(core.settings.database, archive, key)
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock(),
                                  retention_cutoff=core.clock())
    state = tmp_path / "restored-state"
    restore(archive, empty_database, key, core.settings.erasure_ledger, state_directory=state)
    with Core(core.settings.model_copy(update={"database": empty_database,
                                              "state_directory": str(state)}),
              clock=core.clock) as recovered:
        for current in (core, recovered):
            row = current.store.one("SELECT event_time_json FROM memories WHERE memory_id=%s",
                                    (mid,))
            assert row["event_time_json"] is None
            assert recall(current, scope("occurred"))["state"] == "NO_RECORD"
            assert recall(current, None)["facts"][0]["evidence_ids"] == ["evt_test_2"]
