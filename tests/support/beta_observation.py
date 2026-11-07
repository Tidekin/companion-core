import json
from pathlib import Path


def paid_fixture(core):
    """Synthetic accounting rows only, never a real admission or provider receipt."""
    usage = core.store.one("SELECT request_id FROM model_usage")
    core.store.insert("model_budget_windows", window_id="synthetic", contract_sha256="a" * 64)
    core.store.insert(
        "model_admissions", request_id=usage["request_id"], logical_request_id="synthetic",
        attempt_id="synthetic-attempt", attempt_number=1, window_id="synthetic",
        contract_sha256="a" * 64, payload_sha256="b" * 64, snapshot_json="{}",
        reserved_micro_usd=100, input_bound=100, output_bound=100, lease_token="synthetic",
        lease_until=core.clock(), io_started_at=core.clock(), journal_path="PRIVATE_PATH",
        journal_id="synthetic-journal",
    )


def feedback_template():
    return json.loads((Path(__file__).resolve().parents[2]
                       / "docs/qq-beta/p11-beta-feedback.template.json").read_text("utf-8"))
