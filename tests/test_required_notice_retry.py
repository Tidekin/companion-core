"""Required wording may use one new settled attempt; optional sharing may still skip."""

from datetime import timedelta

import httpx
import pytest

from companion import owner_authority
from companion.contracts import DeliveryResult, LeaseRequest
from companion.goals import CommitmentRequest, GoalProposal, GoalScope
from companion.model import ModelClient
from companion.owner_authority_protocol import AuthorityStop
from companion.transport import begin_send, delivery_result, lease_deliveries
from tests.conftest import ingest, run
from tests.notice_owner_fixtures import activate_notice_authority, run_notice
from tests.support.goals import result_for, setup_goal
from tests.support.notices import NoticeModel


def failed_commitment(core, tmp_path=None, **paid_limits):
    setup_goal(core, commitment=True)
    core.clock.advance(86401)
    if tmp_path is not None:
        activate_notice_authority(core, tmp_path, **paid_limits)
    assert core.goals.claim() is None
    return core.store.one("SELECT * FROM jobs WHERE kind='activity'")


@pytest.mark.parametrize("outcome", ["failed", "fulfilled"])
def test_skip_gets_one_new_native_attempt_and_retains_both_charges(core, outcome):
    if outcome == "failed":
        job = failed_commitment(core)
    else:
        goal = setup_goal(core, commitment=True)
        step = core.goals.claim()
        core.goals.finish(step, result_for(core, goal))
        job = core.store.one("SELECT * FROM jobs WHERE kind='activity'")
    assert job["max_attempts"] == 1
    core.model = NoticeModel("[SKIP]")
    assert run(run_notice(core))
    first = core.store.one("SELECT * FROM model_usage")
    current = core.store.one('SELECT * FROM jobs WHERE job_id=%s', (job["job_id"],))
    assert (current["state"], current["attempts"], current["max_attempts"]) == ("RETRY_WAIT", 1, 2)
    assert first["state"] == "SETTLED" and first["billed_micro_usd"] > 0
    assert not core.store.rows("SELECT * FROM outbox")
    assert not run(run_notice(core))

    core.clock.advance(6)
    core.model = NoticeModel(
        "这次未能在约定时间内完成阅读。" if outcome == "failed" else "资料已经读到了。"
    )
    assert run(run_notice(core))
    usages = core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence')
    assert len(usages) == 2 and usages[0] == first
    assert len({row["request_id"] for row in usages}) == 2
    assert {row["job_id"] for row in usages} == {job["job_id"]}
    assert all(row["state"] == "SETTLED" and row["billed_micro_usd"] > 0 for row in usages)
    assert core.store.one("SELECT state FROM commitments")["state"] == "IN_PROGRESS"
    for _ in range(1 if outcome == "failed" else 2):
        assert core.store.one("SELECT state FROM commitments")["state"] == "IN_PROGRESS"
        item = lease_deliveries(
            core.store, core.settings, LeaseRequest(worker_id="synthetic"), core.clock()
        )["items"][0]
        begin_send(core.store, item["message_id"], item["lease_token"], core.clock(), core.settings)
        delivery_result(
            core.store,
            core.settings,
            item["message_id"],
            DeliveryResult(lease_token=item["lease_token"], result="SENT"),
            core.clock(),
        )
    assert core.store.one("SELECT state FROM commitments")["state"] == (
        "EXPLAINED_FAILURE" if outcome == "failed" else "FULFILLED"
    )
    assert not run(run_notice(core))


@pytest.mark.parametrize(
    "first",
    [
        "skip",
        "empty",
        "empty_null_calls",
        "empty_list_calls",
        "empty_dict_calls",
        "empty_string_calls",
        "empty_false_calls",
        "empty_zero_calls",
        "missing_usage",
        "empty_missing_usage",
        "oversized_empty",
        "bad_usage",
        "malformed",
        "missing_content",
        "timeout",
    ],
)
def test_only_received_empty_required_reply_gets_one_more_attempt(core, tmp_path, first):
    job = failed_commitment(core, tmp_path)
    calls = []

    def provider(request):
        calls.append(request)
        if len(calls) == 1 and first == "timeout":
            raise httpx.ReadTimeout("synthetic unreceived response", request=request)
        body = {
            "choices": [{"message": {"content": "这次没有按时完成阅读。"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
        if len(calls) == 1:
            body["choices"][0]["message"]["content"] = (
                " " if first.startswith("empty") else "[SKIP]"
            )
            if first.endswith("_calls"):
                body["choices"][0]["message"]["tool_calls"] = {
                    "empty_null_calls": None, "empty_list_calls": [],
                    "empty_dict_calls": {}, "empty_string_calls": "",
                    "empty_false_calls": False, "empty_zero_calls": 0,
                }[first]
            if first == "oversized_empty":
                body["choices"][0]["message"]["content"] = " " * 32001
            if first in {"missing_usage", "empty_missing_usage"}:
                del body["usage"]
            elif first == "bad_usage":
                body["usage"] = {"prompt_tokens": True, "completion_tokens": 20}
            elif first == "malformed":
                body["choices"] = []
            elif first == "missing_content":
                body["choices"] = [{"message": {}}]
        return httpx.Response(200, json=body)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
            core.model = ModelClient(core.settings, transport)
            assert await run_notice(core)
            before = core.store.rows("SELECT * FROM model_usage")
            known = first in {"skip", "empty", "empty_null_calls", "empty_list_calls"}
            current = core.store.one('SELECT * FROM jobs WHERE job_id=%s', (job["job_id"],))
            assert current["state"] == ("RETRY_WAIT" if known else "FAILED")
            assert current["max_attempts"] == (2 if known else 1)
            if first.endswith("_calls"):
                assert current["last_error_code"] == (
                    "MODEL_EMPTY_RESPONSE" if known else "MODEL_INVALID_RESPONSE"
                )
                assert before[0]["state"] == "SETTLED"
            core.clock.advance(6)
            assert await run_notice(core) is known
            assert len(calls) == (2 if known else 1)
            after = core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence')
            assert after[0] == before[0]
            admissions = core.store.rows('SELECT * FROM model_admissions ORDER BY record_sequence')
            assert {row["logical_request_id"] for row in admissions} == {job["job_id"]}
            assert [row["attempt_number"] for row in admissions] == ([1, 2] if known else [1])
            assert len({row["request_id"] for row in admissions}) == len(admissions)
            if known:
                assert all(row["state"] == "SETTLED" for row in after)
                assert core.store.one("SELECT COUNT(*) n FROM outbox")["n"] == 1
            else:
                assert not core.store.rows("SELECT * FROM outbox")

    run(scenario())


@pytest.mark.parametrize(
    "cap",
    ["attempts", "request", "daily", "forget", "source_expiry", "goals_disabled", "unknown_second"],
)
def test_retry_stays_bounded_by_native_attempt_budget_and_source_contracts(core, tmp_path, cap):
    failed_commitment(
        core,
        tmp_path,
        **(
            {"per_request_calls": 1}
            if cap == "request"
            else {"daily_calls": 1}
            if cap == "daily"
            else {}
        ),
    )
    calls = []

    def provider(request):
        calls.append(request)
        if cap == "unknown_second" and len(calls) == 2:
            raise httpx.ReadTimeout("synthetic second response unknown", request=request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "[SKIP]"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
            core.model = ModelClient(core.settings, transport)
            assert await run_notice(core)
            before = core.store.one("SELECT * FROM model_usage")
            if cap == "forget":
                with core.store.transaction(control_write=True):
                    core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
            elif cap == "source_expiry":
                core.clock.advance(core.settings.raw_event_retention_days * 86400 + 1)
            elif cap == "goals_disabled":
                core.settings.goals_enabled = False
            core.clock.advance(6)
            await run_notice(core)
            core.clock.advance(60)
            assert not await run_notice(core)
            assert len(calls) == (2 if cap in {"attempts", "unknown_second"} else 1)
            assert core.store.rows(
                'SELECT * FROM model_usage ORDER BY record_sequence'
            )[0] == before
            assert not core.store.rows("SELECT * FROM outbox")
            assert not core.store.rows("SELECT * FROM jobs WHERE state='RETRY_WAIT'")

    run(scenario())


@pytest.mark.parametrize("boundary", ["open", "stop", "expiry"])
def test_known_skip_keeps_owner_authority_active_and_retry_respects_its_gate(
    core, tmp_path, boundary
):
    core.settings.exploration_enabled = True
    ingest(core, 1, "请稍后读资料")
    core.store.execute(
        "UPDATE jobs SET state='CANCELLED',last_error_code='SYNTHETIC_BACKGROUND_ONLY'"
    )
    core.settings.goals_enabled = True
    expires_at = core.clock.now + timedelta(seconds=300)
    authority = activate_notice_authority(core, tmp_path, expires_at=expires_at)
    core.goals.accept_commitment(
        CommitmentRequest(
            proposal=GoalProposal(
                topic="阅读资料",
                source_event_ids=["evt_test_1"],
                scope=GoalScope(operation="read_content", url="https://example.org/a"),
            ),
            accepted_event_id="evt_test_1",
            deadline=core.clock.now + timedelta(seconds=60),
        )
    )
    core.clock.advance(61)
    assert core.goals.claim() is None
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "[SKIP]" if len(calls) == 1 else "这次未能按时完成阅读。"
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
            core.model = ModelClient(core.settings, transport)
            assert await run_notice(core)
            assert (
                core.store.one("SELECT state FROM jobs WHERE kind='activity'")["state"]
                == "RETRY_WAIT"
            )
            assert owner_authority.status(core.store, core.settings, core.clock)["state"] == (
                "ACTIVE")
            if boundary == "stop":
                owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                    operation_id="stop-required-notice", authority=authority,
                ))
            elif boundary == "expiry":
                core.clock.now = expires_at
            core.clock.advance(6)
            await run_notice(core)
            assert len(calls) == (2 if boundary == "open" else 1)
            assert len(core.store.rows("SELECT * FROM outbox")) == int(boundary == "open")

    run(scenario())
