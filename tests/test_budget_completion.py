"""The calling seam owns settlement; background work yields before reserving liability."""

import asyncio
import json

import httpx
import pytest

from companion import budget, paid_budget
from companion.contracts import CoreError
from companion.model import ModelClient, ModelResponse
from companion.store import after
from tests.conftest import ingest, run
from tests.owner_model_fixtures import authorized_job
from tests.support.paid_model import approved_live
from tests.test_consolidation import enable_nightly
from tests.test_diary import DAY, generate, seed_day

MESSAGES = [{"role": "user", "content": "synthetic completion"}]


@pytest.mark.parametrize("live", [False, True])
def test_complete_returns_only_after_settling_usage(core, tmp_path, live):
    if live:
        approved_live(core, tmp_path)
    request_id = budget.reserve(core.store, core.settings, authorized_job(core),
        "chat", MESSAGES, core.clock())

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}],
                       "usage": {"prompt_tokens": 80, "completion_tokens": 3}},
        ))) as client:
            response = await budget.complete(
                ModelClient(core.settings, client) if live else core.model,
                core.store, core.settings, request_id, MESSAGES, "chat", core.clock,
            )
            usage = core.store.one("SELECT * FROM model_usage WHERE request_id=?", (request_id,))
            assert usage["state"] == "SETTLED"
            assert (usage["input_tokens"], usage["output_tokens"]) == (
                response.input_tokens, response.output_tokens,
            )
            assert usage["billed_micro_usd"] == budget.cost(
                core.settings, response.input_tokens, response.output_tokens,
                response.cached_tokens,
            )

    run(exercise())


@pytest.mark.parametrize("outcome,failure", [
    (httpx.ReadTimeout("synthetic secret prompt"), "ReadTimeout"),
    (httpx.RemoteProtocolError("synthetic secret prompt"), "RemoteProtocolError"),
    (httpx.Response(503, text="synthetic secret prompt"), "HTTP_503"),
    (httpx.Response(400, json={"error": "synthetic secret prompt"}), "HTTP_400"),
])
def test_unknown_transport_outcome_records_only_its_failure_class(core, tmp_path, outcome,
                                                                   failure):
    approved_live(core, tmp_path)
    request_id = budget.reserve(core.store, core.settings, authorized_job(core),
        "chat", MESSAGES, core.clock())

    def provider(request):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            with pytest.raises(CoreError, match="MODEL_RETRYABLE|MODEL_REJECTED"):
                await budget.complete(ModelClient(core.settings, client), core.store,
                                      core.settings, request_id, MESSAGES, "chat", core.clock)

    run(exercise())
    assert core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"
    row = core.store.one("SELECT safe_detail_json FROM audit_log WHERE "
                         "action='model.usage_diagnostic' AND entity_id=?", (request_id,))
    assert json.loads(row["safe_detail_json"]) == {
        "code": "TRANSPORT_USAGE_UNKNOWN", "actual_usage_known": False,
        "transport_failure": failure, "cache_usage_present": None,
    }
    assert "secret" not in row["safe_detail_json"]


@pytest.mark.parametrize("failure", ["transport", "cancelled", "expired"])
def test_failed_completion_retains_reservation_without_caller_cleanup(core, tmp_path, failure):
    approval = approved_live(core, tmp_path)
    # Keep the independent job lease alive while probing model admission expiry.
    job_id = authorized_job(core, lease_seconds=core.settings.lease_seconds * 2)
    request_id = budget.reserve(core.store, core.settings, job_id,
        "chat", MESSAGES, core.clock())
    reserved = core.store.one("SELECT billed_micro_usd FROM model_usage")["billed_micro_usd"]
    calls = []

    async def exercise():
        entered = asyncio.Event()

        async def provider(request):
            calls.append(request)
            entered.set()
            if failure == "transport":
                raise httpx.ReadTimeout("synthetic interruption")
            await asyncio.Future()

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            if failure == "expired":
                core.clock.advance(core.settings.lease_seconds + 1)
            invocation = budget.complete(
                ModelClient(core.settings, client), core.store, core.settings,
                request_id, MESSAGES, "chat", core.clock,
            )
            if failure == "cancelled":
                task = asyncio.create_task(invocation)
                await asyncio.wait_for(entered.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(CoreError, match="JOB_LEASE_LOST"
                                   if failure == "expired" else "MODEL_RETRYABLE"):
                    await invocation

    run(exercise())
    if failure == "expired":
        assert core.store.one("SELECT lease_until FROM jobs WHERE job_id=?",
                              (job_id,))["lease_until"] > core.clock()
    assert len(calls) == (0 if failure == "expired" else 1)
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
        "state": "UNKNOWN", "billed_micro_usd": reserved,
    }
    with core.store.transaction():
        paid_budget.journal(core.store, approval, allow_settlement=True)


@pytest.mark.parametrize("scenario", ["diary", "reflection"])
def test_business_binding_failure_settles_before_the_calling_seam(core, monkeypatch, scenario):
    model = seed_day(core) if scenario == "diary" else enable_nightly(core)
    original = budget.reserve

    def reserve_then_expire(*args, **kwargs):
        request_id = original(*args, **kwargs)
        core.clock.advance(core.settings.lease_seconds + 1)
        return request_id

    monkeypatch.setattr(budget, "reserve", reserve_then_expire)
    generate(core) if scenario == "diary" else run(core.reflections.run_day(DAY))
    assert model.calls == []
    usage = core.store.one("SELECT * FROM model_usage WHERE scenario=?", (scenario,))
    assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] > 0


def test_journal_history_uses_constant_query_count_and_still_checks_missing_rows(core, tmp_path):
    approval = approved_live(core, tmp_path, per_request_calls=10)
    counts = []
    request_ids = []
    for index in range(8):
        request_id = budget.reserve(core.store, core.settings, authorized_job(core),
            "chat", MESSAGES, core.clock())
        request_ids.append(request_id)
        budget.settle(core.store, core.settings, request_id, ModelResponse("ok", 80, 3))
        if index in {0, 7}:
            queries = []
            core.store.db.set_trace_callback(queries.append)
            try:
                with core.store.transaction():
                    paid_budget.journal(core.store, approval)
            finally:
                core.store.db.set_trace_callback(None)
            counts.append(sum(query.startswith("SELECT") for query in queries))
    assert counts[1] == counts[0]
    # A LEFT JOIN must retain an admission even when its usage projection is lost.
    core.store.execute("PRAGMA foreign_keys=OFF")
    core.store.execute("DELETE FROM model_usage WHERE request_id=?", (request_ids[0],))
    core.store.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(CoreError, match="MODEL_BUDGET_RECONCILIATION_REQUIRED"):
        with core.store.transaction():
            paid_budget.journal(core.store, approval)


@pytest.mark.parametrize("scenario", ["memory_extract", "memory_review"])
@pytest.mark.parametrize("state", ["QUEUED", "RUNNING", "RETRY_WAIT"])
def test_background_memory_yields_to_runnable_chat_before_any_reservation(core, scenario, state):
    ingest(core)
    core.store.execute("UPDATE jobs SET state=?,next_attempt_at=?", (state, core.clock()))
    with pytest.raises(CoreError, match="BACKGROUND_YIELD_TO_CHAT"):
        budget.reserve(core.store, core.settings, None, scenario, MESSAGES, core.clock())
    assert core.store.rows("SELECT * FROM model_usage") == []


@pytest.mark.parametrize("reason", ["manual", "erased"])
def test_background_priority_keeps_manual_and_nonrunnable_work_available(core, reason):
    job_id = ingest(core)["job_id"]
    if reason == "erased":
        core.store.execute("UPDATE events SET redacted=1")
    request_id = budget.reserve(
        core.store, core.settings, job_id if reason == "manual" else None,
        "memory_review", MESSAGES, core.clock(),
    )
    assert core.store.one("SELECT state FROM model_usage WHERE request_id=?", (request_id,)) == {
        "state": "RESERVED",
    }


def test_chat_waiting_for_tools_keeps_background_yielding(core):
    ingest(core)
    core.store.execute("UPDATE jobs SET state='RETRY_WAIT',next_attempt_at=?",
                       (after(core.clock(), 60),))
    with pytest.raises(CoreError, match="BACKGROUND_YIELD_TO_CHAT"):
        budget.reserve(core.store, core.settings, None, "memory_review", MESSAGES, core.clock())
    assert not core.store.rows("SELECT * FROM model_usage")


def test_extraction_yield_leaves_the_batch_and_retry_count_untouched(core):
    seed_day(core)
    core.settings.memory_enabled = True
    core.settings.extraction_idle_seconds = 0
    core.settings.extraction_min_events = 3
    ingest(core, 4, "new chat takes priority")

    assert not run(core.extract_once())
    assert core.model.calls == []
    assert core.store.rows("SELECT * FROM model_usage") == []
    assert not core.store.one("SELECT 1 FROM events WHERE memory_processed=1")
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key LIKE 'extract_attempts:%'")
