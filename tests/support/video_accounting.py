import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from companion import video_accounting
from companion.config import Settings
from companion.core import Core
from companion.legacy_import import SOURCE_REVISION, import_sqlite_backup, migrate_sqlite
from tests.conftest import Clock, FakeModel
from tests.postgres_fixture import isolated_database


@pytest.fixture
def core(tmp_path, migrated_database):
    """Upgrade actual frozen v1 evidence; never execute a retired capture workflow."""
    fixture_path = Path(__file__).parents[1] / "fixtures/video-rate-defect-v1.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    database = tmp_path / "history.db"
    migrate_sqlite(database, fixture["revision"])
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        for table in fixture["tables"]:
            connection.execute('DELETE FROM "' + table + '"')
        for table, rows in fixture["tables"].items():
            columns = list(rows[0])
            sql = ('INSERT INTO "' + table + '" ('
                   + ','.join('"' + key + '"' for key in columns)
                   + ') VALUES (' + ','.join('?' for _ in columns) + ')')
            connection.executemany(sql, [[row[key] for key in columns] for row in rows])
    migrate_sqlite(database, SOURCE_REVISION)
    journal = tmp_path / "original-budget.jsonl"
    journal.write_text(fixture["journal"], encoding="utf-8")
    (tmp_path / "privacy.jsonl").write_text(fixture["erasure_ledger"], encoding="utf-8")
    with isolated_database(template=migrated_database) as target:
        import_sqlite_backup(database, target)
        settings = Settings.model_validate({**fixture["settings"], "database": target,
            "state_directory": str(tmp_path / "state"),
            "embedding_cache_dir": os.environ.get(
                "COMPANION_TEST_EMBEDDING_CACHE", "data/embedding-cache"),
            "erasure_ledger": str(tmp_path / "privacy.jsonl"),
            "bridge_token": "test-token-" * 4, "model_key": "synthetic-key",
            "video_model_key": "synthetic-video-provider-key-12345",
            "observation_adapters": {name: {**value, "token": "synthetic-history-" + name * 4}
                for name, value in fixture["settings"]["observation_adapters"].items()},
            "paid_model": {**fixture["settings"]["paid_model"], "journal_path": str(journal)}})
        clock = Clock()
        clock.now = datetime.fromisoformat(fixture["clock"])
        with Core(settings, FakeModel(), clock) as instance:
            yield instance


@pytest.fixture
def failed_video(core):
    row = core.store.one("SELECT * FROM model_usage WHERE scenario='content_video'")
    assert row["state"] == "UNKNOWN" and row["input_tokens"] == 100
    path = Path(core.settings.paid_model.journal_path)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    request = {"request_id": row["request_id"], "journal_id": lines[0]["journal_id"],
        "expected_head_sha256": lines[-1]["sha256"], "authorization_sha256": "a" * 64,
        "deployment_sha256": "b" * 64, "idempotency_key": "c" * 64}
    return request, path, row


def correct(core, request):
    return video_accounting.reconcile(core.store, core.settings, request, core.clock)
