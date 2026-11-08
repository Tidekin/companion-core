"""Slow write transactions report where they were opened, and nothing else."""

import json

import psycopg
import pytest

from companion import telemetry
from companion.store import Store


@pytest.fixture
def store(core, monkeypatch):
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    core.store.execute("CREATE TABLE t(v TEXT)")
    return core.store


def records(capsys):
    captured = capsys.readouterr()
    assert '"db_write_lock"' not in captured.out  # stdout stays clean for CLI receipts.
    return [json.loads(line) for line in captured.err.splitlines()
            if '"db_write_lock"' in line]


def test_fast_transactions_are_silent(store, capsys):
    with store.transaction():
        store.execute("INSERT INTO t VALUES ('secret value')")
    assert records(capsys) == []


def test_slow_commit_and_rollback_report_site_and_timing_only(store, capsys, monkeypatch):
    monkeypatch.setattr(telemetry, "LOCK_REPORT_MS", 0)
    with store.transaction():
        store.execute("INSERT INTO t VALUES ('secret value')")
    with pytest.raises(RuntimeError), store.transaction():
        raise RuntimeError("business failure")
    first, second = records(capsys)
    assert first["site"].startswith(
        "test_store_lock_telemetry:test_slow_commit_and_rollback_report_site_and_timing_only:")
    assert (first["outcome"], second["outcome"]) == ("COMMIT", "ROLLBACK")
    assert set(first) == {"schema_version", "evidence_type", "session", "stage", "site",
                          "wait_ms", "held_ms", "outcome"}
    assert "secret" not in json.dumps([first, second])


def test_busy_wait_is_reported_and_the_error_is_unchanged(store, tmp_path, capsys,
                                                          monkeypatch):
    monkeypatch.setattr(telemetry, "LOCK_REPORT_MS", 0)
    other = Store(store.database, store.state_directory)
    try:
        with store.transaction(control_write=True):
            with pytest.raises(psycopg.errors.LockNotAvailable), other.transaction():
                pass
    finally:
        other.close()
    busy = [r for r in records(capsys) if r["outcome"] == "ROLLBACK"]
    assert len(busy) == 1 and busy[0]["wait_ms"] >= 4000 and busy[0]["held_ms"] == 0


def test_disabled_telemetry_emits_nothing(store, capsys, monkeypatch):
    monkeypatch.setattr(telemetry, "LOCK_REPORT_MS", 0)
    monkeypatch.delenv("COMPANION_TELEMETRY")
    with store.transaction():
        pass
    assert records(capsys) == []


def test_full_capture_includes_fast_transactions_for_complete_percentiles(
    store, capsys, monkeypatch,
):
    monkeypatch.setenv("COMPANION_TELEMETRY_ALL_LOCKS", "1")
    with store.transaction():
        store.execute("INSERT INTO t VALUES ('secret value')")
    rows = records(capsys)
    assert len(rows) == 1 and rows[0]["outcome"] == "COMMIT"
    assert rows[0]["wait_ms"] >= 0 and rows[0]["held_ms"] >= 0
    assert "secret" not in json.dumps(rows)
