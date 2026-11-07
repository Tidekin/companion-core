"""Only diary work refused before a model reservation can defer safely."""

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from companion import budget, owner_authority
from companion.contracts import CoreError
from companion.core import Core
from companion.model import ModelClient, ModelResponse
from companion.owner_authority_protocol import AuthorityStop
from tests.conftest import run
from tests.native_continuous_support import frame, host
from tests.support.diary import DiaryModel


@asynccontextmanager
async def native_day(core, tmp_path, *, daily_calls=10):
    async def source(ws):
        for number in range(1, 4):
            await ws.send(frame(number, text=f"今天讨论了第{number}项读书安排。"))
        await ws.wait_closed()

    async with host(core, tmp_path, source, ticking=False,
        purposes=("chat", "history", "memory", "diary"),
        paid_options={"daily_calls": daily_calls, "concurrency": 1},
        core_options={"diary_enabled": True, "diary_min_source_events": 3},
    ) as (bridge, _, sends, calls):
        await bridge.capture(3)
        await bridge.flush()
        assert await core.run_once()
        assert await bridge.pump()
        assert len(calls) == len(sends) == 1
        local = core.clock.now.astimezone(ZoneInfo(core.settings.timezone))
        day = local.date()
        core.clock.now = (local + timedelta(days=1)).replace(hour=4)
        yield bridge, day


@pytest.mark.parametrize("outcome", ["success", "exhausted", "unknown"])
def test_received_empty_diary_preserves_bounded_retry(core, tmp_path, outcome):
    draft_model, calls = DiaryModel(), []

    async def provider(request):
        calls.append(request)
        text = " "
        if len(calls) > 1 and outcome == "success":
            payload = json.loads(request.content)
            text = (await draft_model.complete(payload["messages"], "diary")).text
        body = {"choices": [{"message": {"content": text}}]}
        if outcome != "unknown":
            body["usage"] = {"prompt_tokens": 140, "completion_tokens": 80}
        return httpx.Response(200, json=body)

    async def scenario():
        async with native_day(core, tmp_path) as (_, day):
            original_outbox = core.store.rows("SELECT * FROM outbox")
            original_usage = core.store.rows("SELECT * FROM model_usage")
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
                core.diary.model = ModelClient(core.settings, transport)
                core.diary.schedule_day(day)
                assert await core.diary.generate_once("empty-first")
                first = core.store.one("SELECT * FROM model_usage WHERE scenario='diary'")
                row = core.store.one("SELECT * FROM diary_runs")
                assert row["last_error_code"] == "MODEL_EMPTY_RESPONSE"
                assert row["state"] == ("FAILED" if outcome == "unknown" else "RETRY_WAIT")
                assert first["state"] == ("UNKNOWN" if outcome == "unknown" else "SETTLED")
                for _ in range(core.settings.diary_max_attempts):
                    core.clock.advance(3600)
                    await core.diary.generate_once("empty-recovery")
                usages = core.store.rows(
                    "SELECT * FROM model_usage WHERE scenario='diary' ORDER BY record_sequence")
                assert usages[0] == first
                expected = 1 if outcome == "unknown" else (
                    2 if outcome == "success" else core.settings.diary_max_attempts)
                assert len(calls) == len(usages) == expected
                assert len({row["request_id"] for row in usages}) == expected
                assert core.store.one("SELECT state FROM diary_runs")["state"] == (
                    "COMMITTED" if outcome == "success" else "FAILED")
                assert core.store.rows("SELECT * FROM outbox") == original_outbox
                for usage in original_usage:
                    assert core.store.one('SELECT * FROM model_usage WHERE request_id=%s',
                                          (usage["request_id"],)) == usage
    run(scenario())


@pytest.mark.parametrize("recovery", ["settled", "unknown", "stop", "day_reset"])
def test_diary_admission_refusal_defers_without_losing_attempts_or_replaying_unknown(
    core, tmp_path, monkeypatch, recovery,
):
    draft_model = DiaryModel()

    async def scenario():
        async with native_day(core, tmp_path,
                              daily_calls=1 if recovery == "day_reset" else 10) as (bridge, day):
            original = core.diary.repo.claim
            competing = []

            def claim_then_compete(worker_id, **kwargs):
                claimed = original(worker_id, **kwargs)
                if claimed:
                    request = budget.reserve(core.store, core.settings, None, "memory_extract",
                        [{"role": "user", "content": "synthetic competitor"}], core.clock(),
                        clock=core.clock)
                    competing.append(request)
                    if recovery == "day_reset":
                        budget.settle(core.store, core.settings, request,
                                      ModelResponse("", 10, 1, 0))
                return claimed

            core.diary.schedule_day(day)
            core.store.execute("UPDATE diary_runs SET attempts=max_attempts")
            monkeypatch.setattr(core.diary.repo, "claim", claim_then_compete)
            assert await core.diary.generate_once("admission-race")
            row = core.store.one("SELECT * FROM diary_runs")
            assert row["state"] == "RETRY_WAIT" and row["model_request_id"] is None
            assert row["attempts"] == row["max_attempts"]
            assert row["last_error_code"] == (
                "MODEL_DAILY_BUDGET_EXHAUSTED" if recovery == "day_reset"
                else "MODEL_PROVIDER_CONCURRENCY_EXHAUSTED")
            assert draft_model.calls == []
            monkeypatch.setattr(core.diary.repo, "claim", original)
            if recovery == "unknown":
                dispatched = []

                def lost_response(request):
                    dispatched.append(request)
                    raise httpx.ReadTimeout("original competitor outcome unavailable")

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lost_response),
                ) as original_http:
                    with pytest.raises(CoreError):
                        await budget.complete(ModelClient(core.settings, original_http),
                            core.store, core.settings, competing[0],
                            [{"role": "user", "content": "synthetic competitor"}],
                            "memory_extract", core.clock)
                assert len(dispatched) == 1
            elif recovery != "day_reset":
                budget.settle(core.store, core.settings, competing[0],
                              ModelResponse("", 10, 1, 0))
            if recovery == "stop":
                owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                    operation_id="stop-diary-admission", authority=bridge.runtime.authority))
            core.clock.advance(31)
            if recovery == "day_reset":
                assert not await core.diary.generate_once("before-reset")
                core.clock.advance(24 * 3600)

            async def provider(request):
                payload = json.loads(request.content)
                result = await draft_model.complete(payload["messages"], "diary")
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": result.text}}],
                    "usage": {"prompt_tokens": 140, "completion_tokens": 80}})

            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model:
                with Core(core.settings, ModelClient(core.settings, model), core.clock) as worker:
                    assert worker.store is not core.store
                    worker.diary.schedule_day(day)
                    worked = await worker.diary.generate_once("after-recovery")
                    assert worked is (recovery != "stop")
                    row = worker.store.one("SELECT state,attempts FROM diary_runs")
                    if recovery in {"settled", "day_reset"}:
                        assert row["state"] == "COMMITTED"
                        assert row["attempts"] == core.settings.diary_max_attempts + 1
                        assert len(draft_model.calls) == 1
                    else:
                        assert draft_model.calls == []
                        assert row["state"] == ("CANCELLED" if recovery == "stop" else "RETRY_WAIT")
                    if recovery == "unknown":
                        assert worker.store.one('SELECT state FROM model_usage WHERE request_id=%s',
                                                (competing[0],))["state"] == "UNKNOWN"
                        assert worker.store.one(
                            "SELECT model_request_id,last_error_code FROM diary_runs") == {
                                "model_request_id": None,
                                "last_error_code": "MODEL_PROVIDER_CONCURRENCY_EXHAUSTED"}
                        assert not await worker.diary.generate_once("unknown-not-replayed")
    run(scenario())
