import sqlite3
from contextlib import closing

import pytest

from companion.store import Store, migrate
from companion.voice_protocol import VoiceLeaseRef
from tests.conftest import ingest
from tests.support.voice_jobs import claim, create_tts, prepared
from tests.test_test_fixtures import historical_database


def test_singing_migration_preserves_tool_foreign_keys_and_actual_job_delete(core, tmp_path):
    ingest(core)
    job = core.store.one("SELECT job_id FROM jobs")["job_id"]
    core.store.insert("model_usage", usage_id="retained-unknown", request_id="retained-request",
        scenario="chat", provider="synthetic", model="synthetic", billed_micro_usd=23,
        occurred_at=core.clock(), state="UNKNOWN")
    core.store.insert("agent_tool_calls", rowid=41, job_id=job, tool_call_id="original-tool",
        tool_name="read_content", args_sha256="a" * 64, model_request_id="retained-request",
        state="UNKNOWN", metadata_json='{"original":"retained"}',
        created_at=core.clock(), updated_at=core.clock())
    path = historical_database(core.store, tmp_path / "tool-custody.sqlite3",
                               "0060_mood_appraisals")

    def foreign_keys(store, table):
        fields = ("table", "from", "to", "on_update", "on_delete", "match")
        return {tuple(row[key] for key in fields)
                for row in store.rows(f"PRAGMA foreign_key_list({table})")}

    with closing(Store(path)) as previous:
        before = {table: foreign_keys(previous, table)
                  for table in ("voice_jobs", "agent_tool_calls")}
        calls = previous.rows("SELECT rowid AS original_rowid,* FROM agent_tool_calls")
        usage = previous.rows("SELECT * FROM model_usage")
    migrate(path, "0061_singing_voice")
    with closing(Store(path)) as current:
        for table, keys in before.items():
            assert keys <= foreign_keys(current, table)
        assert ("voice_jobs", "voice_job_id", "job_id", "NO ACTION", "NO ACTION", "NONE") in (
            foreign_keys(current, "agent_tool_calls"))
        after = current.rows("SELECT rowid AS original_rowid,* FROM agent_tool_calls")
        assert all(row.pop("voice_job_id") is None for row in after)
        assert after == calls
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            current.execute("UPDATE agent_tool_calls SET tool_name='sing_song',"
                            "voice_job_id='missing'")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            current.execute("UPDATE agent_tool_calls SET state='LOST'")
        current.execute("DELETE FROM jobs WHERE job_id=?", (job,))
        assert current.rows("SELECT * FROM agent_tool_calls") == []
        assert current.rows("SELECT * FROM model_usage") == usage
        assert current.rows("PRAGMA foreign_key_check") == []


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
