from companion import cloud_budget
from companion.cloud_budget import CloudBudgetConfig
from companion.store import uid

MESSAGES = [{"role": "user", "content": "synthetic budget request"}]


def currency_blocks(store, settings, now):
    from companion.capabilities import snapshot
    return [reason for item in snapshot(store, settings, lambda: now)["capabilities"]
            for reason in item["reason_codes"] if reason.startswith("CLOUD_")]


def options(core, **changes):
    config = CloudBudgetConfig(**changes)
    object.__setattr__(core.settings, "cloud_budget", config)
    return config


def native(store, reference_id, amount, now, *, state="RESERVED", scenario="chat"):
    store.insert("model_usage", usage_id=uid("usage"), request_id=reference_id,
                 scenario=scenario, provider="synthetic", model="synthetic",
                 billed_micro_usd=amount, state=state, pricing_known=1, occurred_at=now)


def charge(core, reference_id, amount, *, store=None, now=None, task_kind=None, task_id=None):
    store, now = store or core.store, now or core.clock()
    with store.transaction():
        result = cloud_budget.reserve(
            store, core.settings, reference_type="model", reference_id=reference_id,
            estimated_micro_usd=amount, now=now, task_kind=task_kind, task_id=task_id,
        )
        native(store, reference_id, amount, now)
    return result


def change(core, key="cloud-change-1", **values):
    current = cloud_budget.policy(core.store, core.settings, core.clock())
    return cloud_budget.configure(
        core.store, core.settings, {"expected_version": current["version"], "changes": values},
        idempotency_key=key, now=core.clock(),
    )
