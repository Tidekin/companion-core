"""One explicit renewal of legacy money-bearing contracts; original bytes stay intact."""

from companion.contracts import CoreError
from companion_delivery_protocol import digest

PAID_FIELDS = frozenset({
    "daily_micro_usd", "local_monthly_micro_usd", "monthly_micro_usd",
    "per_attempt_micro_usd", "per_request_micro_usd", "reflection_monthly_micro_usd",
    "scenario_daily_micro_usd", "window_micro_usd",
})
CAPABILITY_FIELDS = {
    "meme": frozenset({"meme_caption_daily_micro_usd"}),
    "rem": frozenset({"daily_micro_usd", "monthly_micro_usd"}),
}


def previous_approval(settings, old, new, old_policy, new_policy, evidence):
    """Verify hash-bound old configurations before dropping only their retired fields.

    The caller first binds `old` to the original paid-contract digest. Its normal
    renewal validator then compares the returned copy with the new approval.
    No permission, quota, price, expiry or historical journal value is rewritten.
    """
    from companion import owner_authority

    before, after = old_policy.capability_policies, new_policy.capability_policies
    changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    if (
        not PAID_FIELDS <= old.keys()
        or PAID_FIELDS & new.keys()
        or old.get("concurrency") != new.get("concurrency")
        or old.get("concurrency_basis") != new.get("concurrency_basis")
        or before.keys() != after.keys()
        or not isinstance(evidence, dict)
        or set(evidence) != changed
        or changed - CAPABILITY_FIELDS.keys()
    ):
        raise CoreError("OWNER_MODEL_CARRY_APPROVAL_CHANGED", 409)
    for capability in changed:
        value = evidence[capability]
        removed = CAPABILITY_FIELDS[capability]
        if (
            not isinstance(value, dict)
            or not removed <= value.keys()
            or digest(value) != before[capability]
            or digest({key: item for key, item in value.items() if key not in removed})
            != after[capability]
        ):
            raise CoreError("OWNER_MODEL_CARRY_APPROVAL_CHANGED", 409)
    owner_authority._check_policy(settings, new_policy)
    return {key: value for key, value in old.items() if key not in PAID_FIELDS}
