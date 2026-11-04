"""Every goal evidence source revokes its notice through the real recall boundary."""

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest

from companion import api, notices, owner_authority
from companion.core import Core
from companion.goals import CommitmentRequest, GoalProposal, GoalScope
from companion.model import ModelClient
from companion.owner_authority_protocol import OwnerCapture, OwnerGrantPolicy
from companion.qq_rich import input_digest
from companion.store import after
from companion_delivery_protocol.authority import capture_signing_payload
from tests.conftest import run
from tests.support.owner_authority import activate
from tests.test_input_interrupt_bridge import model_response
from tests.test_owner_input import captured, inbound
from tests.test_owner_recall import receipt


@asynccontextmanager
async def owner_environment(core, tmp_path):
    capture, key = captured(core, tmp_path)
    original = owner_authority.current(core.store, core.settings, core.clock)
    policy = OwnerGrantPolicy.model_validate({**original.policy.model_dump(),
        "purposes": ["chat", "goals", "history"],
        "history": {"scope": "owner_private", "not_before": after(core.clock(), -60),
                    "max_turns": 15, "max_utf8_bytes": 16000}})
    authority, _ = activate(core, policy, revision=1, operation="notice-source-authority")
    core.settings.goals_enabled = core.settings.exploration_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    app = api.create_app(core.settings)
    with Core(core.settings, core.model, core.clock) as api_core:
        app.state.core = api_core
        assert api_core.store is not core.store
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1", headers={"Authorization": "Bearer " +
                core.settings.bridge_token.get_secret_value()}) as client:
            yield client, capture, key, authority["authority"]


async def admit_owner(client, core, template, key, authority, number, text):
    value = template.model_dump(mode="json", exclude={"signature"})
    source = template.source_event_id.rsplit(":", 1)[0] + ":" + str(number)
    value.update(authority=authority, source_event_id=source,
                 event_id="evt_" + hashlib.sha256(source.encode()).hexdigest(),
                 input_sha256=input_digest({"text": text}))
    value["signature"] = key.sign(capture_signing_payload(value)).hex()
    capture = OwnerCapture.model_validate(value)
    event = inbound(core, capture)
    event = event.model_copy(update={"payload": event.payload.model_copy(update={"text": text})})
    approved = await client.post("/internal/v1/owner-authority/inputs/approve", json=value)
    assert approved.status_code == 200, approved.text
    result = await client.post("/internal/v1/inbox/events", json=event.model_dump(mode="json"),
                               headers={"X-Idempotency-Key": event.event_id})
    assert result.status_code == 202, result.text
    job = core.store.one("SELECT authority_json FROM jobs WHERE job_id=?",
                         (result.json()["job_id"],))
    assert json.loads(job["authority_json"]) == authority
    return event.model_dump(mode="json"), capture


@pytest.mark.parametrize("recall_at", ["queued", "provider", "ready"])
def test_additional_goal_evidence_recall_cancels_notice_and_keeps_authority_active(
    core, tmp_path, recall_at,
):
    async def scenario():
        async with owner_environment(core, tmp_path) as (client, template, key, authority):
            provider_calls = []
            recall_body = None

            async def recall():
                nonlocal recall_body
                recall_body = receipt(core, captures[0], key).model_dump(mode="json")
                response = await client.post("/internal/v1/owner-authority/inputs/recall",
                                              json=recall_body)
                assert response.status_code == 200, response.text
                assert response.json()["state"] == "RECALLED", response.text
                assert response.json()["duplicate"] is False

            async def provider(request):
                messages = json.loads(request.content)["messages"]
                if '"kind":"commitment"' in messages[-1]["content"]:
                    provider_calls.append("notice")
                    if recall_at == "provider":
                        await recall()
                    return model_response("这次没能在约定时间内完成资料阅读。")
                return model_response("我收到这条请求了。")

            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model_http:
                core.model = ModelClient(core.settings, model_http)
                inputs, captures = [], []
                for number, text in [(1, "这份资料的主题是合成天文项目"),
                                     (2, "请稍后帮我阅读并通知结果")]:
                    event, capture = await admit_owner(
                        client, core, template, key, authority, number, text)
                    captures.append(capture)
                    inputs.append(event)
                    assert await core.run_once()
                commitment = core.goals.accept_commitment(CommitmentRequest(
                    proposal=GoalProposal(topic="合成天文项目",
                        source_event_ids=[event["event_id"] for event in inputs],
                        scope=GoalScope(operation="read_content",
                            url="https://www.bilibili.com/video/BV1xx411c7mD")),
                    accepted_event_id=inputs[1]["event_id"],
                    deadline=core.clock.now + timedelta(seconds=60),
                ))
                core.clock.advance(61)
                assert core.goals.claim() is None
                job = core.store.one("SELECT * FROM jobs WHERE kind='activity'")
                assert json.loads(job["authority_json"]) == authority
                expected_sources = {event["event_id"] for event in inputs}
                assert {row["source_event_id"] for row in core.store.rows(
                    "SELECT source_event_id FROM event_dependencies WHERE event_id=?",
                    (job["event_id"],),
                )} == expected_sources
                assert {row["event_id"] for row in core.store.rows(
                    "SELECT event_id FROM job_sources WHERE job_id=?", (job["job_id"],),
                )} == expected_sources | {job["event_id"]}
                if recall_at == "queued":
                    await recall()
                with Core(core.settings, core.model, core.clock) as worker:
                    assert worker.store is not core.store
                    assert await worker.run_once(
                        "notice-source-closure", job_id=job["job_id"],
                    ) is (recall_at != "queued")
                if recall_at == "ready":
                    assert core.store.one("SELECT state FROM outbox WHERE origin_event_id=?",
                                          (job["event_id"],)) == {"state": "PENDING"}
                    await recall()

                assert core.store.one("SELECT state,last_error_code FROM jobs WHERE job_id=?",
                                      (job["job_id"],)) == (
                    {"state": "SUCCEEDED", "last_error_code": None} if recall_at == "ready" else
                    {"state": "CANCELLED", "last_error_code": "PRIVACY_CHANGED"}
                )
                assert core.store.one("SELECT redacted,body_json FROM events WHERE event_id=?",
                                      (job["event_id"],)) == {"redacted": 1, "body_json": "{}"}
                assert core.store.one("SELECT state,redacted FROM goals") == {
                    "state": "CANCELLED", "redacted": 1,
                }
                assert core.store.one("SELECT state FROM commitments") == {"state": "ERASED"}
                assert not core.store.rows("SELECT * FROM outbox WHERE origin_event_id=? "
                                           "AND state<>'CANCELLED'", (job["event_id"],))
                usage = core.store.rows("SELECT state,billed_micro_usd FROM model_usage "
                                        "WHERE scenario='activity'")
                assert usage == ([] if recall_at == "queued" else [
                    {"state": "SETTLED", "billed_micro_usd": 140},
                ])
                assert provider_calls == ([] if recall_at == "queued" else ["notice"])
                assert owner_authority.current(core.store, core.settings, core.clock,
                    purpose="goals").authority.as_dict() == authority

                erased_ledger = core.ledger.path.read_bytes()
                repeated = await client.post("/internal/v1/owner-authority/inputs/recall",
                                             json=recall_body)
                assert repeated.status_code == 200 and repeated.json()["duplicate"]
                assert core.ledger.path.read_bytes() == erased_ledger
                assert core.store.one("SELECT redacted FROM events WHERE event_id=?",
                                      (inputs[0]["event_id"],)) == {"redacted": 1}
                saved = core.store.one("SELECT authority_json FROM jobs WHERE job_id=?",
                                       (job["job_id"],))
                assert json.loads(saved["authority_json"]) == authority
                conversation_id = core.store.one("SELECT conversation_id FROM events "
                    "WHERE event_id=?", (job["event_id"],))["conversation_id"]
                assert notices.schedule(core.store, core.settings, "commitment",
                    commitment["commitment_id"], conversation_id, core.clock()) == (
                        job["job_id"])
                with Core(core.settings, core.model, core.clock) as worker:
                    assert await worker.run_once(
                        "notice-recall-replay", job_id=job["job_id"],
                    ) is False
                assert core.store.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='activity'")[
                    "n"] == 1
                assert owner_authority.current(core.store, core.settings, core.clock,
                    purpose="goals").authority.as_dict() == authority

    run(scenario())
