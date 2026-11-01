import sqlite3
from contextlib import closing

import pytest

from companion.store import Store, migrate
from companion.voice_protocol import VoiceLeaseRef
from tests.test_test_fixtures import historical_database
from tests.test_voice_jobs import claim, create_tts, prepared


@pytest.mark.parametrize("started", [False, True])
def test_singing_migration_retains_original_voice_and_uncertain_custody(
    core, tmp_path, monkeypatch, started,
):
    jobs, message, _, _ = prepared(core, monkeypatch)
    create_tts(core, jobs, message)
    _, answer = claim(jobs)
    if started:
        jobs.input("local", VoiceLeaseRef.from_lease(answer.lease))
    path = historical_database(core.store, tmp_path / "old.sqlite3", "0060_mood_appraisals")
    tables = ("voice_jobs", "voice_node_state", "outbox", "model_usage", "runtime_meta")
    with closing(Store(path)) as before_store:
        before = {name: before_store.rows(f"SELECT * FROM {name} ORDER BY rowid")
                  for name in tables}
    migrate(path, "0061_singing_voice")
    with closing(Store(path)) as current:
        after = {name: current.rows(f"SELECT * FROM {name} ORDER BY rowid") for name in tables}
        for row in after["voice_jobs"]:
            assert row.pop("singing_context_json") == "{}"
            assert row.pop("reserved_output_bytes") == 0
            assert row.pop("source_budget_at") is None
            for field in ("source_reserved_requests", "source_reserved_bytes"):
                assert row.pop(field) == 0
            for field in ("source_used_requests", "source_used_bytes"):
                assert row.pop(field) is None
        assert after == before
        assert current.rows("PRAGMA foreign_key_check") == []
        for invalid in ("[]", "null", "not-json"):
            with pytest.raises(sqlite3.IntegrityError):
                current.execute("UPDATE voice_jobs SET singing_context_json=?", (invalid,))
        with pytest.raises(sqlite3.IntegrityError):
            current.execute("UPDATE voice_jobs SET operation='unbounded-shell'")
        for column, invalid in (("content_retired", 2), ("cleanup_state", "UNSAFE"),
                                ("authority_json", "[]"), ("source_partial", 7),
                                ("config_json", "not-json"), ("generation", 0)):
            with pytest.raises(sqlite3.IntegrityError):
                current.execute(f"UPDATE voice_jobs SET {column}=?", (invalid,))
