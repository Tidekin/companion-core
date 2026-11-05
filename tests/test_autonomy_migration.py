"""Upgrade both ordinary databases and databases with the former manual Autonomy SQL."""

from contextlib import closing

import pytest

from companion.store import Store, migrate

TABLES = ("exploration_strategies", "interest_priorities", "interest_portfolio",
          "reflection_insights", "portfolio_changes")


@pytest.mark.parametrize("standalone", [False, True])
def test_upgrade_retires_only_the_untracked_autonomy_copies(tmp_path, standalone):
    path = str(tmp_path / "previous.db")
    migrate(path, "0053_owner_jargon")
    with closing(Store(path)) as store:
        store.execute("INSERT INTO runtime_meta VALUES ('migration-native-evidence','retained')")
        store.execute("INSERT INTO jargon_terms VALUES "
                      "('保留说法','[]',1,'CANDIDATE',NULL,NULL,'before','before')")
        if standalone:
            for table in TABLES:
                store.execute(f"CREATE TABLE {table} (private_text TEXT NOT NULL)")
                store.execute(f"INSERT INTO {table} VALUES (?)", ("retired-private-text-3129",))
    migrate(path)
    migrate(path)  # Normal startup and restore can apply the same head repeatedly.
    with closing(Store(path)) as store:
        assert store.one("SELECT version_num FROM alembic_version")["version_num"] == (
            "0065_bili_grant_expiry"
        )
        native = store.one("SELECT value FROM runtime_meta WHERE key='migration-native-evidence'")
        assert native == {
            "value": "retained",
        }
        assert store.one("SELECT term FROM jargon_terms")["term"] == "保留说法"
        assert all(not store.one("SELECT 1 FROM sqlite_master WHERE name=?", (table,))
                   for table in TABLES)
        assert store.rows("PRAGMA foreign_key_check") == []
    assert b"retired-private-text-3129" not in (tmp_path / "previous.db").read_bytes()
