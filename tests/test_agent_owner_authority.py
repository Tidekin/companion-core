"""SDK boundary checks against real native grants, jobs, source receipts and STOP."""

import asyncio

import pytest

from companion import owner_authority
from companion.agent_runtime import check_conversation
from companion.agent_tools import core_query_tools
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from tests.support.owner_authority import activate
from tests.support.owner_runtime import chat


@pytest.fixture
def owner_chat(core, tmp_path):
    capture, job = chat(core, tmp_path)
    return core, capture, job


def test_chat_tools_do_not_expose_ungranted_purposes(owner_chat):
    core, _, job = owner_chat
    tools = core_query_tools(core, job, [])
    assert set(tools.tools) == {
        "current_time", "capability_status", "search_meme_wiki"}
    result = asyncio.run(tools.tools["current_time"].function())
    assert result["timezone"] == core.settings.timezone
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_PURPOSE_REQUIRED"):
        check_conversation(core, job, purpose="memory")
    assert not core.store.rows("SELECT * FROM model_usage")


@pytest.mark.parametrize("boundary", ["stop", "revision", "lease", "source"])
def test_prepared_tool_rechecks_native_authority_before_execution(owner_chat, boundary):
    core, capture, job = owner_chat
    tools = core_query_tools(core, job, [])
    if boundary == "stop":
        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id="stop-sdk-tool-authority", authority=capture.authority))
    elif boundary == "revision":
        policy = owner_authority.current(core.store, core.settings, core.clock).policy
        core.store.execute("UPDATE jobs SET state='CANCELLED' WHERE job_id=%s",
                           (job["job_id"],))
        activate(core, policy, revision=1, operation="revise-sdk-tool-authority")
    elif boundary == "lease":
        core.store.execute("UPDATE jobs SET lease_token='different' WHERE job_id=%s",
                           (job["job_id"],))
    else:
        core.store.execute('UPDATE events SET redacted=1 WHERE event_id=%s', (job["event_id"],))
    with pytest.raises(CoreError):
        asyncio.run(tools.tools["current_time"].function())
    assert not core.store.rows("SELECT * FROM model_usage")
    assert not core.store.rows("SELECT * FROM outbox")
