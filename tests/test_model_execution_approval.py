"""Execution approval retains accounting, resource bounds and lost-response custody."""

import httpx
import pytest

from companion import budget, paid_budget
from companion.contracts import CoreError
from companion.model import ModelClient, ModelResponse
from tests.conftest import run
from tests.owner_model_fixtures import authorized_job
from tests.support.paid_model import approved_live

MESSAGES = [{"role": "user", "content": "synthetic owner request"}]


@pytest.mark.parametrize("scenario", ["chat", "memory_review"])
def test_approved_calls_record_and_settle_real_dispatch(core, tmp_path, scenario):
    approval = approved_live(core, tmp_path)
    job_id = authorized_job(core, purposes=("chat", "memory") if scenario == "memory_review"
                            else ("chat",))
    calls = []

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: (
            calls.append(request) or httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 3},
            })
        ))) as client:
            for _ in range(2):
                request_id = budget.reserve(core.store, core.settings, job_id, scenario,
                                            MESSAGES, core.clock())
                await budget.complete(ModelClient(core.settings, client), core.store,
                                      core.settings, request_id, MESSAGES, scenario, core.clock)

    run(exercise())
    assert len(calls) == 2
    rows = core.store.rows("SELECT state,billed_micro_usd FROM model_usage")
    assert all(row == {"state": "SETTLED", "billed_micro_usd": 86} for row in rows)
    assert core.store.one("SELECT COUNT(*) n FROM model_admissions")["n"] == 2
    assert core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'") is None
    with core.store.transaction():
        assert paid_budget.journal(core.store, approval)["head_sha256"] != "0" * 64




@pytest.mark.parametrize("changes,code", [
    ({"max_input_tokens": 1}, "MODEL_INPUT_LIMIT"),
])
def test_input_bounds_apply_before_model_admission(core, tmp_path, changes, code):
    approved_live(core, tmp_path, **changes)
    job_id = authorized_job(core)
    with pytest.raises(CoreError, match=code):
        budget.reserve(core.store, core.settings, job_id, "chat", MESSAGES, core.clock())
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_lost_response_stops_replaying_its_original_request(core, tmp_path):
    approved_live(core, tmp_path, concurrency=1)
    job_id = authorized_job(core)
    request_id = budget.reserve(core.store, core.settings, job_id, "chat", MESSAGES, core.clock())
    budget.settle(core.store, core.settings, request_id)
    with pytest.raises(CoreError, match="MODEL_OUTCOME_UNKNOWN"):
        budget.reserve(core.store, core.settings, job_id, "chat", MESSAGES, core.clock())
    assert core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"


def test_settlement_uses_original_authority_after_configuration_changes(core, tmp_path):
    approval = approved_live(core, tmp_path)
    job_id = authorized_job(core)
    request_id = budget.reserve(core.store, core.settings, job_id, "chat", MESSAGES, core.clock())
    core.settings.paid_model = approval.model_copy(
        update={"window_calls": approval.window_calls + 1})
    budget.settle(core.store, core.settings, request_id, ModelResponse("ok", 80, 3))
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    assert core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'") is None
