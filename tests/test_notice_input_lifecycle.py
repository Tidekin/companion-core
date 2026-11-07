"""Combined notice/reply lifecycle through authenticated capture and independent workers."""

import json
from datetime import timedelta

import httpx
import pytest

from companion.core import Core
from companion.goals import CommitmentRequest, GoalProposal, GoalScope
from companion.model import ModelClient
from companion_onebot_bridge import owner_notices
from tests.conftest import run
from tests.native_continuous_support import frame, host, recall_frame
from tests.support.input_interrupt_bridge import model_response
from tests.support.private_companion import companion_options


@pytest.mark.parametrize("recall_at", [None, "before_dispatch", "after_dispatch"])
def test_required_notice_tracks_actual_sources_and_drains_recalled_work(
    core, tmp_path, recall_at,
):
    async def source(ws):
        await ws.send(frame(1, text="请帮我稍后阅读这条资料"))
        await ws.wait_closed()

    async def scenario():
        async with host(core, tmp_path, source,
            purposes=("chat", "history", "memory", "goals", "proactive", "content", "exploration"),
            core_options=companion_options(core)["core_options"],
            paid_options={"per_request_calls": 3}) as (window, _, sends, _):
            client = window.client
            provider_calls = []

            def notice_request(request):
                return '"kind":"commitment"' in json.loads(request.content)["messages"][-1][
                    "content"]

            async def recall():
                owner_notices.put(window.runtime,
                    recall_frame(1, time=int(core.clock.now.timestamp())),
                                    core.clock.now, ("127.0.0.1", 12345))
                assert await owner_notices.flush_one(window.runtime, client)

            async def before(request):
                if notice_request(request) and recall_at == "before_dispatch":
                    await recall()

            async def provider(request):
                if notice_request(request):
                    provider_calls.append("notice")
                    if recall_at == "after_dispatch":
                        await recall()
                    return model_response("这次没能在约定时间内读完资料。")
                return model_response("我记下这个请求了。")

            async with httpx.AsyncClient(transport=httpx.MockTransport(provider),
                                         event_hooks={"request": [before]}) as model_http:
                core.model = ModelClient(core.settings, model_http)
                await window.capture()
                await window.flush()
                assert await core.run_once()
                assert await window.pump()
                owner_id = core.store.one("SELECT event_id FROM events WHERE actor_id='owner'")[
                    "event_id"]
                core.goals.accept_commitment(CommitmentRequest(
                    proposal=GoalProposal(topic="阅读公开资料", source_event_ids=[owner_id],
                        scope=GoalScope(operation="read_content",
                            url="https://www.bilibili.com/video/BV1xx411c7mD")),
                    accepted_event_id=owner_id, deadline=core.clock.now + timedelta(seconds=60),
                ))
                core.clock.advance(61)
                assert core.goals.claim() is None
                notice_job = core.store.one("SELECT * FROM jobs WHERE kind='activity'")
                assert notice_job and notice_job["state"] == "QUEUED"
                with Core(core.settings, core.model, core.clock) as worker:
                    assert worker.store is not core.store
                    assert await worker.run_once("notice-integration", job_id=notice_job["job_id"])
                job = core.store.one("SELECT * FROM jobs WHERE job_id=?", (notice_job["job_id"],))
                assert job["context_tracked"] == 1
                assert {row["event_id"] for row in core.store.rows(
                    "SELECT event_id FROM job_sources WHERE job_id=?", (job["job_id"],),
                )} == {owner_id, job["event_id"]}
                usage = core.store.one("SELECT state,billed_micro_usd FROM model_usage "
                                       "WHERE scenario='activity'")
                assert usage["state"] == "SETTLED"
                if recall_at:
                    assert job["state"] == "CANCELLED"
                    assert not core.store.rows("SELECT * FROM outbox WHERE origin_event_id=?",
                                               (job["event_id"],))
                    assert len(sends) == 1
                    assert provider_calls == ([] if recall_at == "before_dispatch" else ["notice"])
                    assert (usage["billed_micro_usd"] == 0) is (recall_at == "before_dispatch")
                else:
                    assert job["state"] == "SUCCEEDED", job
                    assert await window.pump()
                    assert len(sends) == 2 and provider_calls == ["notice"]
                    assert core.store.one("SELECT state FROM commitments")["state"] == (
                        "EXPLAINED_FAILURE")
                assert window.runtime.row()["state"] == "ACTIVE"

    run(scenario())
