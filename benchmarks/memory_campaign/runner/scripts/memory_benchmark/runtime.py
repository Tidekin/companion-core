"""Run-scoped configuration and native approval; credentials stay outside artifacts."""

import hashlib
import json
import os
import secrets
from pathlib import Path


def context():
    path = os.environ.get("COMPANION_MEMORY_BENCHMARK_RUN")
    if not path:
        raise RuntimeError("Use scripts.memory_benchmark.cli run to prepare an isolated run")
    return json.loads(Path(path).read_text("utf-8"))


def credential(config):
    location = config["credentials"]
    if location.get("key_file"):
        value = Path(location["key_file"]).read_text("utf-8-sig").strip()
    else:
        value = ""
        for line in Path(location["env_file"]).read_text("utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, content = line.split("=", 1)
                if name.strip() == location["env_name"]:
                    value = content.strip().strip("\"'")
    if not value or any(c.isspace() for c in value):
        raise ValueError("Credential file must contain one key, or the named dotenv value")
    return value


def native_settings(directory):
    # Imported only inside the worker, after its frozen source is placed first.
    from companion.config import PaidModelApproval, Settings
    from companion.owner_authority_protocol import OwnerRuntimeConfig

    config = context()
    provider, price = config["provider"], config["pricing"]
    rates = {
        name: max(1, round(price[name] * price["safety_factor"] * 100000))
        for name in ("input_rate", "cached_input_rate", "output_rate")
    }
    cap = round(config["budget"]["inference_cny"] / 10 * 1_000_000)
    authorization = hashlib.sha256(
        json.dumps(
            {
                "run_id": config["run_id"],
                "source_commit": config["source_commit"],
                "user_action": "explicit CLI run of synthetic memory benchmark",
                "total_cap_cny": config["budget"]["total_cny"],
                "concurrency_basis": "local_active_attempts_v2",
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    paid = PaidModelApproval(
        live_approved=True,
        window_id="memory-benchmark-" + config["run_id"],
        approval_sha256=authorization,
        provider=provider["adapter"],
        model=provider["model"],
        endpoint=provider["base_url"],
        currency="USD",
        timezone="Asia/Shanghai",
        starts_at=config["logical_starts_at"],
        expires_at=config["logical_expires_at"],
        rates_valid_until=config["logical_expires_at"],
        **rates,
        max_input_tokens=40000,
        max_output_tokens=4096,
        per_attempt_micro_usd=20000,
        per_request_micro_usd=100000,
        per_request_calls=8,
        daily_micro_usd=cap,
        daily_calls=1000,
        monthly_micro_usd=cap,
        window_micro_usd=cap,
        window_calls=1000,
        concurrency=1,
        concurrency_basis="local_active_attempts_v2",
        journal_path=str(directory / "paid-budget.jsonl"),
    )
    settings = Settings(
        provider=provider["adapter"],
        model_url=provider["base_url"],
        model_name=provider["model"],
        model_key=credential(config),
        **rates,
        **config.get("native", {}),
        owner_qq_id="10001",
        bot_qq_id="10002",
        bridge_token=secrets.token_urlsafe(32),
        mode="live",
        database=str(directory / "core.sqlite3"),
        erasure_ledger=str(directory / "erasures.jsonl"),
        embedding_cache_dir=config["embedding_cache_dir"],
        owner_runtime=OwnerRuntimeConfig(mode="ENABLED"),
        memory_enabled=True,
        proactive_enabled=False,
        max_recent_messages=12,
        operator_hot_reload=False,
        monthly_limit_micro_usd=cap,
        memory_daily_micro_usd=cap,
        autonomous_daily_micro_usd=cap,
        paid_model=paid,
    )
    return settings, authorization
