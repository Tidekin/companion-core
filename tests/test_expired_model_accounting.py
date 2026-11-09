"""An expired native claim must retire its model through the accounting journal."""

import asyncio

import httpx
import pytest

from companion import budget, owner_authority, paid_budget
from companion.agent_persistence import ConversationStore
from companion.contracts import CoreError
from companion.model import ModelClient
from companion.owner_authority_protocol import AuthorityStop
from tests.support.owner_runtime import chat


@pytest.mark.parametrize("kind", ["chat", "activity"])
def test_expired_claim_keeps_unknown_and_original_journal_consistent(core, tmp_path, kind):
    from pydantic_ai import Conversation

    capture, job = chat(core, tmp_path)
    with core.store.transaction():
        ConversationStore(core, job, [])._save(
            Conversation(conversation_id=job["job_id"]), "MODEL_PENDING")
    messages = core.context(job)
    request_id = budget.reserve(core.store, core.settings, job["job_id"], "chat", messages,
                                core.clock(), clock=core.clock)
    def provider(request):
        raise httpx.ReadTimeout("synthetic interrupted worker", request=request)

    async def interrupted():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            with pytest.raises(CoreError, match="MODEL_RETRYABLE"):
                await budget._dispatch(ModelClient(core.settings, client), core.store,
                    core.settings, request_id, messages, "chat", core.clock, dispatch={})
    asyncio.run(interrupted())
    admission = core.store.one("SELECT * FROM model_admissions WHERE request_id=%s",
                                (request_id,))
    assert admission["io_started_at"] is not None
    usage = core.store.one("SELECT * FROM model_usage WHERE request_id=%s", (request_id,))
    # Both native claim kinds share this expiry path. Retiring the synthetic claim
    # grants no activity permission and does not invoke a provider.
    core.store.execute("UPDATE jobs SET kind=%s WHERE job_id=%s", (kind, job["job_id"]))
    core.clock.advance(core.settings.lease_seconds + 1)
    with core.store.transaction():
        core.recover(core.clock())
    assert core.store.one("SELECT * FROM model_admissions WHERE request_id=%s",
                          (request_id,)) == admission
    assert core.store.one("SELECT * FROM model_usage WHERE request_id=%s", (request_id,)) == {
        **usage, "state": "UNKNOWN"}
    view = paid_budget.journal(core.store, core.settings.paid_model, initialize=False,
                               allow_settlement=True)
    assert view["model_latest"][request_id]["state"] == "UNKNOWN"
    assert not view["blocked_request_ids"]
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")
    assert core.claim_job("after-expiry", job_id=job["job_id"]) is None
    assert core.store.one("SELECT state FROM jobs WHERE job_id=%s",
                          (job["job_id"],))["state"] in {"FAILED", "CANCELLED"}
    assert not core.store.rows("SELECT * FROM outbox")
    stopped = owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="expired-accounting-native-stop", authority=capture.authority))
    assert stopped["state"] == "STOPPED"
