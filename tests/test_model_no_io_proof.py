"""A provider error code or a foreign capability cannot manufacture non-dispatch."""

import httpx
import pytest

from companion import budget, paid_budget
from companion.contracts import CoreError
from companion.model import ModelAdmission, ModelClient, ModelDispatchRefused
from tests.conftest import run
from tests.owner_model_fixtures import authorized_job
from tests.support.paid_model import approved_live


@pytest.mark.parametrize("foreign_proof", [False, True])
def test_delegate_background_error_keeps_uncertainty(core, tmp_path, foreign_proof):
    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "synthetic uncertain provider request"}]
    request_id = budget.reserve(core.store, core.settings, authorized_job(core),
                                "chat", messages, core.clock())
    calls = []

    def provider(request):
        calls.append(request)
        error = CoreError("BACKGROUND_YIELD_TO_CHAT")
        if foreign_proof:
            raise ModelDispatchRefused(error, ModelAdmission("a" * 64))
        raise error

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            with pytest.raises(CoreError, match="BACKGROUND_YIELD_TO_CHAT"):
                await budget.complete(ModelClient(core.settings, client), core.store, core.settings,
                                      request_id, messages, "chat", core.clock)

    run(scenario())
    assert len(calls) == 1
    row = core.store.one("SELECT * FROM model_usage WHERE request_id=?", (request_id,))
    assert row["state"] == "UNKNOWN" and row["billed_micro_usd"] > 0
    assert not core.store.rows("SELECT * FROM audit_log WHERE action='model.not_dispatched'")
    paid_budget.journal(core.store, core.settings.paid_model)
    with pytest.raises(CoreError, match="CLOUD_TASK_USAGE_UNCERTAIN"):
        budget.reserve(core.store, core.settings, authorized_job(core),
                       "chat", messages, core.clock())
