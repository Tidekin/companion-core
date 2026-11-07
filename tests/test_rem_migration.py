"""Adding fictional artifacts must preserve original financial and source custody."""

from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from companion.store import Store, migrate
from tests.conftest import ingest
from tests.support.historical_schema import historical_database
from tests.support.token_measurement_migration import historical_admission, snapshot


def downgrade(path):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1]
                                                 / "migrations"))
    config.set_main_option("sqlalchemy.url", "sqlite:///" + Path(path).as_posix())
    command.downgrade(config, "0041_agent_conversations")


def test_rem_migration_preserves_unknown_costs_and_empty_downgrade(tmp_path):
    database = historical_admission(tmp_path)
    migrate(str(database), "0041_agent_conversations")
    with closing(Store(str(database))) as store:
        before = snapshot(store)
    migrate(str(database), "0042_rem")
    migrate(str(database), "0042_rem")
    with closing(Store(str(database))) as store:
        assert snapshot(store) == before
        assert store.rows("SELECT * FROM rem_records") == []
        assert store.rows("PRAGMA foreign_key_check") == []
    downgrade(database)
    with closing(Store(str(database))) as store:
        assert snapshot(store) == before


def test_retained_rem_artifact_blocks_destructive_downgrade(core, tmp_path):
    ingest(core)
    job = core.store.one("SELECT job_id FROM jobs")
    core.store.insert("rem_records", rem_id="schema-fixture", job_id=job["job_id"],
        local_date="2026-10-19", timezone=core.settings.timezone, config_sha256="a" * 64,
        source_memory_ids_json="[]", source_event_ids_json="[]", source_snapshot_json="{}",
        output_json='{"kind":"FICTION","text":"retained fiction"}',
        created_at=core.clock(), expires_at="2027-01-01T00:00:00+00:00")
    before = core.store.rows("SELECT * FROM rem_records")
    path = historical_database(core.store, tmp_path / "rem-custody.db", "0042_rem")
    with pytest.raises(RuntimeError, match="REM_DOWNGRADE_REQUIRES_EXPLICIT_DATA_REVIEW"):
        downgrade(path)
    with closing(Store(path)) as original:
        assert original.rows("SELECT * FROM rem_records") == before
        assert original.one("SELECT version_num FROM alembic_version")["version_num"] == "0042_rem"
