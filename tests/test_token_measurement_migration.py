"""Historical admission evidence survives adding request measurement provenance."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from companion.store import Store, migrate
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
    with closing(Store(str(path))) as store:
        before = snapshot(store)
        indexes = store.rows("SELECT name,sql FROM sqlite_master WHERE type='index' "
                             "AND tbl_name='model_admissions' ORDER BY name")
        assert not store.schema_capabilities()
    migrate(str(path), REVISION)
    migrate(str(path), REVISION)
    with closing(Store(str(path))) as store:
        after = snapshot(store)
        assert after["model_admissions"][0].pop("measurement_json") == "{}"
        assert after == before
        # Measurement alone does not supply the current native execution schema.
        assert not store.schema_capabilities()
        assert store.one("SELECT version_num FROM alembic_version")["version_num"] == REVISION
        assert store.rows("PRAGMA foreign_key_check") == []
        assert store.rows("SELECT name,sql FROM sqlite_master WHERE type='index' "
                          "AND tbl_name='model_admissions' ORDER BY name") == indexes
    downgrade(path)
    with closing(Store(str(path))) as store:
        assert snapshot(store) == before
        assert store.one("SELECT version_num FROM alembic_version")["version_num"] == PRIOR
    migrate(str(path), REVISION)
    with closing(Store(str(path))) as store:
        assert store.one("SELECT measurement_json FROM model_admissions") == {
            "measurement_json": "{}"}
    migrate(str(path))
    with closing(Store(str(path))) as store:
        current = snapshot(store)
        admission = current["model_admissions"][0]
        assert admission.pop("measurement_json") == "{}"
        assert admission.pop("authority_json") == "{}"
        assert current == before
        assert store.schema_capabilities()
        assert store.rows("PRAGMA foreign_key_check") == []


def test_measured_admission_blocks_evidence_losing_downgrade(tmp_path):
    path = historical_admission(tmp_path)
    migrate(str(path), REVISION)
    with closing(Store(str(path))) as store:
        store.execute("UPDATE model_admissions SET measurement_json=?",
                      ('{"count_kind":"estimate","payload_sha256":"' + "b" * 64 + '"}',))
        before = snapshot(store)
        schema = store.one("SELECT sql FROM sqlite_master WHERE name='model_admissions'")
    with pytest.raises(RuntimeError, match="TOKEN_MEASUREMENT_DOWNGRADE_REQUIRES_EVIDENCE_REVIEW"):
        downgrade(path)
    with closing(Store(str(path))) as store:
        assert snapshot(store) == before
        assert store.one("SELECT sql FROM sqlite_master WHERE name='model_admissions'") == schema
        assert store.one("SELECT version_num FROM alembic_version")["version_num"] == REVISION


@pytest.mark.parametrize("invalid", [None, "not-json", "[]", "null", '"text"'])
def test_measurement_column_rejects_invalid_or_non_object_receipts(tmp_path, invalid):
    path = historical_admission(tmp_path)
    migrate(str(path), REVISION)
    with closing(Store(str(path))) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.execute("UPDATE model_admissions SET measurement_json=?", (invalid,))
        assert store.one("SELECT measurement_json FROM model_admissions") == {
            "measurement_json": "{}"}
