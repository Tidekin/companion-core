"""Shared synthetic paid model setup for component and process tests."""

from pydantic import SecretStr


def approved_live(core, tmp_path, **changes):
    from companion.config import PaidModelApproval

    values = dict(
        live_approved=True,
        window_id="offline-window-1",
        approval_sha256="a" * 64,
        provider="offline-fixture",
        model="offline-text",
        endpoint="https://synthetic.invalid/v1",
        currency="USD",
        timezone="Asia/Shanghai",
        starts_at="2026-10-01T00:00:00+08:00",
        expires_at="2026-12-01T00:00:00+08:00",
        rates_valid_until="2026-12-01T00:00:00+08:00",
        input_rate=1000000,
        cached_input_rate=100000,
        output_rate=2000000,
        max_input_tokens=20000,
        max_output_tokens=1000,
        per_request_calls=3,
        daily_calls=10,
        window_calls=20,
        concurrency=2,
        journal_path=str(tmp_path / "budget.jsonl"),
    )
    values.update(changes)
    core.settings.paid_model = PaidModelApproval.model_validate(values)
    core.settings.mode = "live"
    core.settings.provider = values["provider"]
    core.settings.model_name = values["model"]
    core.settings.model_url = values["endpoint"]
    core.settings.model_key = SecretStr("synthetic-key")
    for name in ("input_rate", "cached_input_rate", "output_rate"):
        setattr(core.settings, name, values[name])
    return core.settings.paid_model
