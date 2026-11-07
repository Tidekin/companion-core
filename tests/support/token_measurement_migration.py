from contextlib import closing

from companion.store import Store, migrate

PRIOR = "0034_voice_retention_v2"


def historical_admission(tmp_path):
    path = tmp_path / "historical.db"
    migrate(str(path), PRIOR)
    with closing(Store(str(path))) as store:
        store.insert("model_usage", rowid=17, usage_id="usage", request_id="request",
                     scenario="chat", provider="synthetic", model="synthetic",
                     billed_micro_usd=23, occurred_at="2026-10-24T00:00:00+00:00",
                     state="UNKNOWN")
        store.insert("model_budget_windows", window_id="window", contract_sha256="a" * 64)
        store.insert("model_admissions", rowid=41, request_id="request",
                     logical_request_id="logical", attempt_id="attempt", attempt_number=1,
                     window_id="window", contract_sha256="a" * 64, payload_sha256="b" * 64,
                     snapshot_json='{"preserve": "exact historical bytes"}',
                     reserved_micro_usd=23, input_bound=100, output_bound=20,
                     lease_token="lease", lease_until="2026-10-24T00:01:00+00:00",
                     journal_path=str(tmp_path / "budget.jsonl"), journal_id="journal")
    return path


def snapshot(store):
    return {table: store.rows(f"SELECT rowid AS original_rowid,* FROM {table} ORDER BY rowid")
            for table in ("model_usage", "model_admissions", "model_budget_windows")}
