"""Pure request counting stays outside the writer and is reused for exact dispatch."""

import httpx
import pytest

from companion import budget
from companion.model import ModelClient
from tests.conftest import run
from tests.owner_model_fixtures import authorized_job
from tests.test_paid_model_gate import approved_live
from tests.test_token_measurement import configure


@pytest.mark.parametrize("exact", [False, True])
def test_dispatch_counts_once_outside_writer_and_reuses_at_transport(
    core, tmp_path, monkeypatch, exact,
):
    (configure if exact else approved_live)(core, tmp_path)
    job = authorized_job(core)
    messages = [{"role": "user", "content": "synthetic preparation"}]
    original, counted, calls = budget.measure_request, [], []

    def measure(*args, **kwargs):
        counted.append(core.store.in_write_transaction)
        return original(*args, **kwargs)

    monkeypatch.setattr(budget, "measure_request", measure)
    request = budget.reserve(core.store, core.settings, job, "chat", messages, core.clock())

    async def scenario():
        def provider(req):
            calls.append(req)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            await budget.complete(ModelClient(core.settings, client), core.store,
                core.settings, request, messages, "chat", core.clock)

    run(scenario())
    assert counted == [False, False]  # Reserve and dispatch, neither holds the writer.
    assert len(calls) == 1
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
