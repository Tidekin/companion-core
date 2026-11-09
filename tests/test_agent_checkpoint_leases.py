"""Progress checkpoints renew only a still-owned, still-live native chat lease."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai.usage import RunUsage

from companion import agent_runtime, model, owner_authority
from companion.agent_persistence import ConversationStore
from companion.agent_tools import core_query_tools
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from companion.store import after
from tests.support.owner_runtime import chat


def checkpoint(persistence):
    return asyncio.run(persistence.before_model_request(
        SimpleNamespace(usage=RunUsage()), SimpleNamespace(messages=[])))


def deadline(core, job):
    return core.store.one("SELECT lease_until FROM jobs WHERE job_id=%s",
                          (job["job_id"],))["lease_until"]


def test_original_checkpoint_renews_a_live_lease_after_validating_sources(core, tmp_path):
    _, job = chat(core, tmp_path)
    original = deadline(core, job)
    core.clock.advance(core.settings.lease_seconds // 2)
    checkpoint(ConversationStore(core, job, []))
    row = core.store.one("SELECT lease_until FROM jobs WHERE job_id=%s", (job["job_id"],))
    assert row["lease_until"] == after(core.clock(), core.settings.lease_seconds)
    assert row["lease_until"] > original
    assert not core.store.rows("SELECT * FROM model_usage")


def test_checkpoint_does_not_renew_a_lease_that_expires_during_source_checks(
    core, tmp_path, monkeypatch,
):
    _, job = chat(core, tmp_path)
    original = deadline(core, job)
    persistence = ConversationStore(core, job, [])
    original_check = persistence.check

    def validate_then_expire(**kwargs):
        original_check(**kwargs)
        core.clock.advance(core.settings.lease_seconds + 1)

    monkeypatch.setattr(persistence, "check", validate_then_expire)
    with pytest.raises(CoreError, match="JOB_LEASE_LOST"):
        checkpoint(persistence)
    row = core.store.one("SELECT lease_until FROM jobs WHERE job_id=%s", (job["job_id"],))
    assert row["lease_until"] == original
    assert persistence.row() is None
    assert not core.store.rows("SELECT * FROM model_usage")


@pytest.mark.parametrize("change", ["state", "token"])
def test_checkpoint_does_not_renew_ownership_changed_during_source_checks(
    core, tmp_path, monkeypatch, change,
):
    _, job = chat(core, tmp_path)
    original = deadline(core, job)
    persistence = ConversationStore(core, job, [])
    original_check = persistence.check

    def validate_then_change(**kwargs):
        original_check(**kwargs)
        if change == "state":
            core.store.execute("UPDATE jobs SET state='CANCELLED' WHERE job_id=%s",
                               (job["job_id"],))
        else:
            core.store.execute("UPDATE jobs SET lease_token='other-owner' WHERE job_id=%s",
                               (job["job_id"],))

    monkeypatch.setattr(persistence, "check", validate_then_change)
    with pytest.raises(CoreError, match="JOB_LEASE_LOST"):
        checkpoint(persistence)
    assert deadline(core, job) == original and persistence.row() is None
    assert not core.store.rows("SELECT * FROM model_usage")


def test_native_sdk_can_finish_owned_rounds_through_initial_claim_expiry(core, tmp_path):
    _, job = chat(core, tmp_path)
    initial_deadline = deadline(core, job)
    snapshots = []
    persistence = ConversationStore(core, job, snapshots)
    requests = []

    async def provider(request):
        requests.append(json.loads(request.content))
        # Each response fits its original admission; their combined work reaches
        # the first claim while the same native worker continues owning the job.
        core.clock.advance(core.settings.lease_seconds / 3)
        message = {"role": "assistant", "content": "done"}
        if len(requests) < 3:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"clock-{len(requests)}", "type": "function", "function": {
                    "name": "current_time", "arguments": "{}"}}]}
        return httpx.Response(200, json={"choices": [{"message": message}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            core.model = model.ModelClient(core.settings, client)
            result = await agent_runtime.run_conversation(
                core, job, core.context(job), snapshots=snapshots, persistence=persistence,
                toolsets=[core_query_tools(core, job, snapshots)])
            assert result.output == "done"

    asyncio.run(scenario())
    assert len(requests) == 3 and core.clock() >= initial_deadline
    assert persistence.row()["phase"] == "OUTPUT"
    assert len(core.store.rows("SELECT * FROM model_usage WHERE state='SETTLED'")) == 3
    assert not core.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("change", ["stop", "source"])
def test_checkpoint_keeps_native_stop_and_source_guards(core, tmp_path, change):
    capture, job = chat(core, tmp_path)
    if change == "stop":
        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id="checkpoint-native-stop", authority=capture.authority))
    else:
        core.store.execute("UPDATE events SET redacted=1 WHERE event_id=%s",
                           (job["event_id"],))
    original = deadline(core, job)
    persistence = ConversationStore(core, job, [])
    with pytest.raises(CoreError):
        checkpoint(persistence)
    assert deadline(core, job) == original and persistence.row() is None
    assert not core.store.rows("SELECT * FROM model_usage")
