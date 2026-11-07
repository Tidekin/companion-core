"""Historical data survives upgrades; irreversible state blocks lossy downgrades."""

from contextlib import closing

import pytest
from alembic import command

from companion.legacy_import import migrate_sqlite
from tests.support.input_reply_migration import config_for
from tests.support.release_migration import (
    TIMES,
    assert_old_columns_unchanged,
    insert,
    open_historical,
    rows,
    seed_legacy,
    table_snapshot,
)


@pytest.mark.parametrize("protected", ["revision", "lineage"])
def test_reply_migration_preserves_history_and_refuses_lossy_downgrade(tmp_path, protected):
    database, _, _ = seed_legacy(tmp_path, "0003_memory_index")
    migrate_sqlite(str(database), "0021_reflection_admission_retry")
    with closing(open_historical(str(database))) as store:
        before = table_snapshot(store, ("events", "jobs", "conversations"))
    migrate_sqlite(str(database), "0022_input_reply_revision")
    with closing(open_historical(str(database))) as store:
        assert_old_columns_unchanged(store, before)
        assert all(row["reply_revision"] == 0 for row in rows(store, "SELECT * FROM events"))
        assert all(row["context_tracked"] == 0 for row in rows(store, "SELECT * FROM jobs"))
        if protected == "revision":
            store.execute("UPDATE events SET reply_revision=1 WHERE event_id='evt_keep'")
        else:
            insert(store, "job_sources", job_id="job_keep", event_id="evt_erase")
        protected_snapshot = table_snapshot(store, ("events", "jobs", "job_sources"))
    with pytest.raises(
        RuntimeError, match="REPLY_REVISION_DOWNGRADE_REQUIRES_EXPLICIT_DATA_REVIEW",
    ):
        command.downgrade(config_for(database), "0021_reflection_admission_retry")
    with closing(open_historical(str(database))) as store:
        assert table_snapshot(store, ("events", "jobs", "job_sources")) == protected_snapshot
        assert rows(store, "PRAGMA foreign_key_check") == []


def test_discovery_migration_keeps_uncertain_resource_liability(tmp_path):
    database, _, _ = seed_legacy(tmp_path, "0003_memory_index")
    migrate_sqlite(str(database), "0022_input_reply_revision")
    with closing(open_historical(str(database))) as store:
        before = table_snapshot(store, ("events", "jobs"))
    migrate_sqlite(str(database), "0023_meme_discovery_reservations")
    with closing(open_historical(str(database))) as store:
        assert_old_columns_unchanged(store, before)
        insert(store, "meme_discovery_reservations", job_id="job_keep", attempt_key="attempt_keep",
                     day="2026-10-18", state="UNKNOWN", reserved_queries=1,
                     reserved_download_bytes=4096, created_at=TIMES["keep"],
                     updated_at=TIMES["keep"])
        reserved = table_snapshot(store, ("meme_discovery_reservations",))
    with pytest.raises(RuntimeError, match="MEME_BUDGET_DOWNGRADE_REQUIRES_EXPLICIT_DATA_REVIEW"):
        command.downgrade(config_for(database), "0022_input_reply_revision")
    with closing(open_historical(str(database))) as store:
        assert table_snapshot(store, ("meme_discovery_reservations",)) == reserved
        assert rows(store, "PRAGMA foreign_key_check") == []
