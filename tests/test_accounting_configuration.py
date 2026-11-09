"""Current configuration removes monetary admission while original evidence stays readable."""

import pytest
from pydantic import ValidationError

from companion import cumulative
from companion.cloud_budget import CloudBudgetConfig
from companion.config import PaidModelApproval, Settings
from companion.executive_protocol import ExecutionEnvelope, ExecutorEndpoint
from companion.gateway_contract import ClientBudget
from companion.media_config import ContentConfig, ImageGenerationConfig, MediaConfig
from companion.rem_protocol import RemConfig
from tests.support.cumulative_budget import policy
from tests.support.paid_model import approved_live


@pytest.mark.parametrize("model,field", [
    (PaidModelApproval, "per_attempt_micro_usd"),
    (PaidModelApproval, "per_request_micro_usd"),
    (PaidModelApproval, "daily_micro_usd"),
    (PaidModelApproval, "monthly_micro_usd"),
    (PaidModelApproval, "window_micro_usd"),
    (PaidModelApproval, "unlimited_period_spend"),
    (Settings, "monthly_limit_micro_usd"),
    (CloudBudgetConfig, "daily_micro_cny"),
    (CloudBudgetConfig, "video_per_item_micro_cny"),
    (CloudBudgetConfig, "asr_per_item_micro_cny"),
    (cumulative.ModelCaps, "micro_usd"),
    (ClientBudget, "max_request_micro_usd"),
    (ClientBudget, "daily_micro_usd"),
    (ExecutorEndpoint, "max_cost_micro_usd"),
    (ExecutionEnvelope, "max_cost_micro_usd"),
    (RemConfig, "daily_micro_usd"),
    (RemConfig, "monthly_micro_usd"),
    (ContentConfig, "article_image_micro_usd"),
    (ContentConfig, "image_daily_micro_usd"),
    (MediaConfig, "daily_micro_usd"),
    (ImageGenerationConfig, "daily_micro_usd"),
])
def test_current_configuration_refuses_removed_monetary_controls(model, field):
    with pytest.raises(ValidationError) as rejected:
        model.model_validate({field: 1})
    assert any(error["type"] == "extra_forbidden" and error["loc"] == (field,)
               for error in rejected.value.errors())


def test_historical_cumulative_policy_keeps_exact_amounts_and_hash(core, tmp_path):
    approved_live(core, tmp_path)
    scope = policy(core)
    original = scope.model_dump(mode="json")
    original["version"] = 1
    for period in ("request", "window", "day", "month", "account"):
        original[period]["micro_usd"] = 123456
    digest = cumulative.digest(original)
    state = cumulative.empty_state()
    cumulative.fold(state, {
        "kind": "quota_enroll", "version": 1, "at": core.clock(),
        "policy": original, "policy_sha256": digest,
        "previous": scope.expected_journal_head_sha256,
        "legacy": {"models": {}, "operations": {}},
    })
    assert state["policy"] == original and state["policy_sha256"] == digest
    assert scope.version == 2 and "micro_usd" not in scope.request.model_dump()
