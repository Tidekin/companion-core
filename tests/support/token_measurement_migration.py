from contextlib import closing

from companion.legacy_import import migrate_sqlite
from tests.support.release_migration import insert, open_historical, rows

PRIOR = "0034_voice_retention_v2"


def historical_admission(tmp_path):
    path = tmp_path / "historical.db"
    migrate_sqlite(path, PRIOR)
    with closing(open_historical(path)) as db:
        insert(db, "model_usage", rowid=17, usage_id="usage", request_id="request",
                     scenario="chat", provider="synthetic", model="synthetic",
                     billed_micro_usd=23, occurred_at="2026-10-24T00:00:00+00:00",
                     state="UNKNOWN")
        insert(db, "model_budget_windows", window_id="window", contract_sha256="a" * 64)
        insert(db, "model_admissions", rowid=41, request_id="request",
                     logical_request_id="logical", attempt_id="attempt", attempt_number=1,
                     window_id="window", contract_sha256="a" * 64, payload_sha256="b" * 64,
                     snapshot_json='{"preserve": "exact historical bytes"}',
                     reserved_micro_usd=23, input_bound=100, output_bound=20,
                     lease_token="lease", lease_until="2026-10-24T00:01:00+00:00",
                     journal_path=str(tmp_path / "budget.jsonl"), journal_id="journal")
    return path


def snapshot(db):
    return {table: rows(db, f"SELECT rowid AS original_rowid,* FROM {table} ORDER BY rowid")
            for table in ("model_usage", "model_admissions", "model_budget_windows")}
