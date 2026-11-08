"""Historical admission evidence survives adding request measurement provenance."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from companion.legacy_import import SOURCE_REVISION, migrate_sqlite
from tests.support.release_migration import one, open_historical, rows
from tests.support.token_measurement_migration import PRIOR, historical_admission, snapshot

REVISION = "0035_token_measurement"


def downgrade(path):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1]
                                                 / "migrations"))
    config.set_main_option("sqlalchemy.url", "sqlite:///" + path.as_posix())
    command.downgrade(config, PRIOR)


def test_upgrade_and_legacy_downgrade_preserve_accounting_and_snapshot_bytes(tmp_path):
    path = historical_admission(tmp_path)
    with closing(open_historical(path)) as store:
        before = snapshot(store)
        indexes = rows(store, "SELECT name,sql FROM sqlite_master WHERE type='index' "
                             "AND tbl_name='model_admissions' ORDER BY name")
        assert "authority_json" not in {r["name"] for r in rows(
            store, "PRAGMA table_info(model_admissions)")}
    migrate_sqlite(path, REVISION)
    migrate_sqlite(path, REVISION)
    with closing(open_historical(path)) as store:
        after = snapshot(store)
        assert after["model_admissions"][0].pop("measurement_json") == "{}"
        assert after == before
        # Measurement alone does not supply the current native execution schema.
        assert "authority_json" not in {r["name"] for r in rows(
            store, "PRAGMA table_info(model_admissions)")}
        assert one(store, "SELECT version_num FROM alembic_version")["version_num"] == REVISION
        assert rows(store, "PRAGMA foreign_key_check") == []
        assert rows(store, "SELECT name,sql FROM sqlite_master WHERE type='index' "
                          "AND tbl_name='model_admissions' ORDER BY name") == indexes
    downgrade(path)
    with closing(open_historical(path)) as store:
        assert snapshot(store) == before
        assert one(store, "SELECT version_num FROM alembic_version")["version_num"] == PRIOR
    migrate_sqlite(path, REVISION)
    with closing(open_historical(path)) as store:
        assert one(store, "SELECT measurement_json FROM model_admissions") == {
            "measurement_json": "{}"}
    migrate_sqlite(path)
    with closing(open_historical(path)) as store:
        current = snapshot(store)
        admission = current["model_admissions"][0]
        assert admission.pop("measurement_json") == "{}"
        assert admission.pop("authority_json") == "{}"
        assert current == before
        assert one(store, "SELECT version_num FROM alembic_version")["version_num"] == (
            SOURCE_REVISION)
        assert {"measurement_json", "authority_json"} <= {r["name"] for r in rows(
            store, "PRAGMA table_info(model_admissions)")}
        assert one(store, "PRAGMA integrity_check") == {"integrity_check": "ok"}
        assert rows(store, "PRAGMA foreign_key_check") == []


def test_measured_admission_blocks_evidence_losing_downgrade(tmp_path):
    path = historical_admission(tmp_path)
    migrate_sqlite(path, REVISION)
    with closing(open_historical(path)) as store:
        store.execute("UPDATE model_admissions SET measurement_json=?",
                      ('{"count_kind":"estimate","payload_sha256":"' + "b" * 64 + '"}',))
        before = snapshot(store)
        schema = one(store, "SELECT sql FROM sqlite_master WHERE name='model_admissions'")
    with pytest.raises(RuntimeError, match="TOKEN_MEASUREMENT_DOWNGRADE_REQUIRES_EVIDENCE_REVIEW"):
        downgrade(path)
    with closing(open_historical(path)) as store:
        assert snapshot(store) == before
        assert one(store, "SELECT sql FROM sqlite_master WHERE name='model_admissions'") == schema
        assert one(store, "SELECT version_num FROM alembic_version")["version_num"] == REVISION


@pytest.mark.parametrize("invalid", [None, "not-json", "[]", "null", '"text"'])
def test_measurement_column_rejects_invalid_or_non_object_receipts(tmp_path, invalid):
    path = historical_admission(tmp_path)
    migrate_sqlite(path, REVISION)
    with closing(open_historical(path)) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.execute("UPDATE model_admissions SET measurement_json=?", (invalid,))
        assert one(store, "SELECT measurement_json FROM model_admissions") == {
            "measurement_json": "{}"}
