"""0044 adds empty authority custody without rewriting historical execution evidence."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from companion.store import Store, migrate
from companion.voice_protocol import VoiceLeaseRef
from tests.support.voice_jobs import claim, create_tts, prepared


@pytest.mark.parametrize("started", [False, True])
def test_0044_preserves_old_job_and_stop_custody_without_backfilling(core, tmp_path,
                                                                 monkeypatch, started):
    jobs, message, _, _ = prepared(core, monkeypatch)
    create_tts(core, jobs, message)
    _, answer = claim(jobs)
    if started:
        jobs.input("local", VoiceLeaseRef.from_lease(answer.lease))
    path = tmp_path / "before-0044.sqlite3"
    migrate(path, "0043_reader_authority")
    # Copy only columns actually present in the real predecessor schema.
    with closing(sqlite3.connect(path)) as destination, destination:
        tables = [r[0] for r in destination.execute("SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version'")]
        for table in tables:
            if table.startswith("memories_fts"):
                continue
            columns = [r[1] for r in destination.execute(f'PRAGMA table_info("{table}")')]
            for row in core.store.rows(f'SELECT * FROM "{table}"'):
                destination.execute(f'INSERT OR REPLACE INTO "{table}" ({",".join(columns)}) '
                    f'VALUES ({",".join("?" for _ in columns)})', tuple(row[c] for c in columns))
    with closing(Store(path)) as prior:
        before = {table: prior.rows(f"SELECT * FROM {table} ORDER BY rowid") for table in (
            "voice_jobs", "voice_node_state", "outbox", "model_usage", "runtime_meta")}
    migrate(path, "0044_voice_owner_authority")
    with closing(Store(path)) as migrated:
        after = {table: migrated.rows(f"SELECT * FROM {table} ORDER BY rowid") for table in before}
        for row in after["voice_jobs"]:
            assert row.pop("authority_json") == "{}"
        assert after == before
        for value in ("[]", "null", "invalid"):
            with pytest.raises(sqlite3.IntegrityError):
                migrated.execute("UPDATE voice_jobs SET authority_json=?", (value,))
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    config.set_main_option("sqlalchemy.url", "sqlite:///" + path.resolve().as_posix())
    with pytest.raises(RuntimeError, match="VOICE_AUTHORITY_DOWNGRADE"):
        command.downgrade(config, "0043_reader_authority")
