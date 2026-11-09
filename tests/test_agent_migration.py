"""Upgrade SDK custody without changing accounting or losing captured evidence."""

from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from companion.legacy_import import migrate_sqlite as migrate
from tests.conftest import ingest
from tests.support.historical_schema import historical_database
from tests.support.release_migration import insert, one, open_historical, rows
from tests.support.token_measurement_migration import historical_admission, snapshot

PRIOR = "0040_owner_authority"
REVISION = "0041_agent_conversations"


def downgrade(database):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1]
                                                 / "migrations"))
    config.set_main_option("sqlalchemy.url", "sqlite:///" + Path(database).as_posix())
    command.downgrade(config, PRIOR)


def test_upgrade_preserves_original_accounting_and_unrelated_metadata(tmp_path):
    database = historical_admission(tmp_path)
    migrate(str(database), PRIOR)
    with closing(open_historical(database)) as store:
        before = snapshot(store)
        insert(store, "runtime_meta", key="unrelated-text", value="non-JSON historical value")
        insert(store, "runtime_meta", key="reading_followup:retired", value='{"original":true}')
        authority = rows(store, "SELECT * FROM owner_authority_revisions")
    migrate(str(database), REVISION)
    migrate(str(database), REVISION)
    with closing(open_historical(database)) as store:
        assert snapshot(store) == before
        assert rows(store, "SELECT * FROM owner_authority_revisions") == authority
        assert one(store, "SELECT value FROM runtime_meta WHERE key='unrelated-text'") == {
            "value": "non-JSON historical value"}
        assert one(store, "SELECT value FROM runtime_meta "
                         "WHERE key='reading_followup:retired'") is None
        assert rows(store, "PRAGMA foreign_key_check") == []
        assert rows(store, "SELECT * FROM agent_conversations") == []
        assert rows(store, "SELECT * FROM captured_images") == []
    downgrade(database)
    with closing(open_historical(database)) as store:
        assert snapshot(store) == before
        assert one(store, "SELECT version_num FROM alembic_version")["version_num"] == PRIOR


@pytest.mark.parametrize("custody", ["conversation", "image"])
def test_native_custody_blocks_evidence_losing_downgrade(core, tmp_path, custody):
    ingest(core)
    now = core.clock()
    if custody == "conversation":
        job = core.store.one("SELECT job_id FROM jobs")
        core.store.insert("agent_conversations", job_id=job["job_id"], phase="READY",
                          conversation_json='{"synthetic":"retained"}',
                          created_at=now, updated_at=now)
        table = "agent_conversations"
    else:
        core.store.insert("content_items", item_id="image-before-chat", owner_event_id="evt_test_1",
                          kind="qq_image", slot=0, state="SUCCEEDED", captured_privacy_epoch=0,
                          deadline=now, created_at=now, updated_at=now)
        core.store.insert("captured_images", item_id="image-before-chat",
                          proof_json='{"synthetic":"signed-original"}', mime="image/png",
                          expires_at=now)
        table = "captured_images"
    path = historical_database(core.store, tmp_path / "agent-custody.db", REVISION)
    with closing(open_historical(path)) as historical:
        before = rows(historical, f"SELECT * FROM {table}")
    with pytest.raises(RuntimeError, match="AGENT_CONVERSATION_DOWNGRADE_REQUIRES"):
        downgrade(path)
    with closing(open_historical(path)) as original:
        assert rows(original, f"SELECT * FROM {table}") == before
        assert one(original, "SELECT version_num FROM alembic_version")["version_num"] == REVISION
