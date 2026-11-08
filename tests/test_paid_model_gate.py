import asyncio
from contextlib import closing

import httpx
import pytest
from pydantic import SecretStr

from companion.contracts import CoreError
from companion.model import ModelClient
from companion.postgres import Database
from tests.owner_model_fixtures import authorized_job
from tests.postgres_fixture import copy_database
from tests.support.paid_model import approved_live


def test_mode_live_and_key_alone_cannot_start_http(core):
    calls = []
    core.settings.mode = "live"
    core.settings.model_key = SecretStr("synthetic-key")

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: (
                    calls.append(req)
                    or httpx.Response(200, json={"choices": [{"message": {"content": "reply"}}]})
                )
            )
        ) as client:
            with pytest.raises(CoreError, match="MODEL_ADMISSION_REQUIRED"):
                await ModelClient(core.settings, client).complete(
                    [{"role": "user", "content": "hello"}], "chat"
                )

    asyncio.run(exercise())
    assert calls == []


def test_reserve_rejects_missing_approval_before_model(core):
    from companion import budget

    core.settings.mode = "live"
    with pytest.raises(CoreError, match="PAID_APPROVAL_REQUIRED"):
        budget.reserve(
            core.store,
            core.settings,
            None,
            "chat",
            [{"role": "user", "content": "hello"}],
            core.clock(),
        )
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_input_hard_limit_blocks_reservation(core, tmp_path):
    from companion import budget

    approved_live(core, tmp_path, max_input_tokens=20)
    with pytest.raises(CoreError, match="MODEL_INPUT_LIMIT"):
        budget.reserve(
            core.store,
            core.settings,
            authorized_job(core),
            "chat",
            [{"role": "user", "content": "x" * 30}],
            core.clock(),
        )
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_approved_call_requires_durable_admission_and_uses_shared_window(core, tmp_path):
    from companion import budget

    approved_live(core, tmp_path)
    calls = []
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: (
                    calls.append(req)
                    or httpx.Response(
                        200,
                        json={
                            "choices": [{"message": {"content": "ok"}}],
                            "usage": {
                                "prompt_tokens": 30,
                                "completion_tokens": 2,
                                "prompt_cache_hit_tokens": 10,
                            },
                        },
                    )
                )
            )
        ) as client:
            response = await budget.complete(
                ModelClient(core.settings, client),
                core.store,
                core.settings,
                request,
                messages,
                "chat",
                core.clock,
            )
            budget.settle(core.store, core.settings, request, response)
            with pytest.raises(CoreError, match="OWNER_AUTHORITY_MODEL_ADMISSION_REQUIRED"):
                await budget.complete(
                    ModelClient(core.settings, client),
                    core.store,
                    core.settings,
                    request,
                    messages,
                    "chat",
                    core.clock,
                )

    asyncio.run(exercise())
    assert len(calls) == 1
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
        "state": "SETTLED",
        "billed_micro_usd": 25,
    }


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"per_attempt_micro_usd": 1}, "ATTEMPT"),
        ({"daily_micro_usd": 1}, "DAILY"),
        ({"monthly_micro_usd": 1}, "MONTHLY"),
        ({"window_micro_usd": 1}, "WINDOW"),
        ({"per_request_micro_usd": 1}, "REQUEST"),
        ({"max_output_tokens": 128}, "OUTPUT"),
        ({"live_approved": False}, "LIVE_OPT_IN_REQUIRED"),
    ],
)
def test_money_is_advisory_and_other_limits_refuse_before_io(core, tmp_path, changes, code):
    from companion import budget

    approved_live(core, tmp_path, **changes)
    def reserve():
        return budget.reserve(core.store, core.settings, authorized_job(core), "chat",
                              [{"role": "user", "content": "hello"}], core.clock())

    if any(name.endswith("micro_usd") for name in changes):
        request = reserve()
        assert core.store.one("SELECT request_id,state FROM model_usage") == {
            "request_id": request, "state": "RESERVED"}
    else:
        with pytest.raises(CoreError, match=code):
            reserve()
        assert core.store.rows("SELECT * FROM model_usage") == []
    assert core.model.calls == []


@pytest.mark.parametrize(
    "scenario", ["chat", "memory_review", "memory_extract", "diary", "reflection", "activity"]
)
def test_all_scenarios_share_daily_call_limit(core, tmp_path, scenario):
    from companion import budget, owner_authority
    from companion.model import ModelResponse

    core.settings.proactive_enabled = True
    core.settings.reflection_daily_micro_usd = 50000
    core.settings.reflection_monthly_micro_usd = 50000
    approved_live(core, tmp_path, daily_calls=1)
    job_id = authorized_job(core, purposes=("chat", "memory", "diary", "reflection", "proactive"))
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(core.store, core.settings, job_id, "chat", messages, core.clock())
    budget.settle(core.store, core.settings, request, ModelResponse("ok", 30, 2))
    target = job_id
    if scenario not in {"chat", "memory_review"}:
        # This accounting unit finishes its synthetic chat before background admission.
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (job_id,))
        target = None
        if scenario == "activity":
            event_id = core.store.one('SELECT event_id FROM jobs WHERE job_id=%s',
                                      (job_id,))["event_id"]
            with core.store.transaction():
                target = core.new_job(event_id, "activity", "budget-activity", core.clock())
                owner_authority.bind_job(core.store, core.settings, core.clock, target,
                                         purpose="proactive")
            assert core.claim_job("budget-activity", job_id=target)
    with pytest.raises(CoreError, match="DAILY"):
        budget.reserve(core.store, core.settings, target, scenario, messages, core.clock())
    assert core.model.calls == []


def test_unknown_keeps_original_cost_and_absent_response_cannot_replay(core, tmp_path):
    from companion import budget

    approval = approved_live(core, tmp_path, concurrency=1)
    messages = [{"role": "user", "content": "hello"}]
    job_id = authorized_job(core, lease_seconds=7200)
    request = budget.reserve(core.store, core.settings, job_id, "chat", messages, core.clock())
    amount = core.store.one("SELECT billed_micro_usd AS n FROM model_usage")["n"]
    budget.settle(core.store, core.settings, request)
    core.clock.advance(3600)
    core.settings.paid_model = core.settings.paid_model.model_copy(
        update={"window_id": "new-window-2"}
    )
    with pytest.raises(CoreError, match="LIVE_TEXT_INITIAL_WORK_MUST_BE_ZERO"):
        authorized_job(core, renew=True)
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_PAID_POLICY_CHANGED"):
        budget.reserve(core.store, core.settings, job_id, "chat", messages, core.clock())
    core.settings.paid_model = approval
    with pytest.raises(CoreError, match="MODEL_OUTCOME_UNKNOWN"):
        budget.reserve(core.store, core.settings, job_id, "chat", messages, core.clock())
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
        "state": "UNKNOWN",
        "billed_micro_usd": amount,
    }


def test_same_request_id_and_two_calls_in_one_logical_request_count(core, tmp_path):
    from companion import budget
    from companion.model import ModelResponse

    approved_live(core, tmp_path, per_request_calls=2)
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(
        core.store,
        core.settings,
        authorized_job(core, purposes=("chat", "memory")),
        "chat",
        messages,
        core.clock(),
        request_id="same-id",
        logical_request_id="one-message",
    )
    with pytest.raises(CoreError, match="ALREADY_RESERVED"):
        budget.reserve(
            core.store,
            core.settings,
            authorized_job(core, purposes=("chat", "memory")),
            "chat",
            messages,
            core.clock(),
            request_id="same-id",
            logical_request_id="one-message",
        )
    budget.settle(core.store, core.settings, request, ModelResponse("ok", 30, 2))
    second = budget.reserve(
        core.store,
        core.settings,
        authorized_job(core, purposes=("chat", "memory")),
        "memory_review",
        messages,
        core.clock(),
        logical_request_id="one-message",
    )
    budget.settle(core.store, core.settings, second, ModelResponse("ok", 30, 2))
    with pytest.raises(CoreError, match="REQUEST"):
        budget.reserve(
            core.store,
            core.settings,
            authorized_job(core, purposes=("chat", "memory")),
            "chat",
            messages,
            core.clock(),
            logical_request_id="one-message",
        )


def test_multi_connection_reservations_are_atomic(core, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from companion import budget
    from companion.store import Store

    approved_live(core, tmp_path, daily_calls=1)
    job_id = authorized_job(core)

    def attempt(_):
        store = Store(core.database, core.settings.state_directory)
        try:
            return budget.reserve(
                store,
                core.settings,
                job_id,
                "chat",
                [{"role": "user", "content": "hello"}],
                core.clock(),
            )
        except CoreError as error:
            return error.code
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        result = list(pool.map(attempt, range(6)))
    assert sum(x.startswith("model_") for x in result) == 1
    assert result.count("MODEL_DAILY_BUDGET_EXHAUSTED") == 5


def test_window_total_survives_month_boundary_and_daily_timezone_rollover(core, tmp_path):
    from datetime import datetime

    from companion import budget
    from companion.model import ModelResponse

    approved_live(core, tmp_path, window_calls=1, daily_calls=1)
    core.clock.now = datetime.fromisoformat("2026-10-31T15:59:59+00:00")
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    budget.settle(core.store, core.settings, request, ModelResponse("ok", 30, 2))
    core.clock.advance(2)
    with pytest.raises(CoreError, match="WINDOW"):
        budget.reserve(core.store, core.settings, authorized_job(core),
                               "chat", messages, core.clock())


@pytest.mark.parametrize(
    "bad_usage", [None, (999999, 1, 0), (30, 9999, 0), (30, 1, 40), (True, 2, 0)]
)
def test_missing_or_anomalous_usage_never_refunds_unknown(core, tmp_path, bad_usage):
    from companion import budget
    from companion.model import ModelResponse

    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    before = core.store.one("SELECT billed_micro_usd AS n FROM model_usage")["n"]
    response = ModelResponse("ok", *bad_usage) if bad_usage else None
    budget.settle(core.store, core.settings, request, response)
    usage = core.store.one("SELECT state,billed_micro_usd FROM model_usage")
    assert usage["state"] == ("SETTLED" if bad_usage == (999999, 1, 0) else "UNKNOWN")
    assert usage["billed_micro_usd"] >= before


def test_price_change_retains_snapshot_and_permanently_blocks_next_admission(core, tmp_path):
    from companion import budget
    from companion.model import ModelResponse

    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    core.settings.input_rate += 1
    budget.settle(core.store, core.settings, request, ModelResponse("ok", 30, 2))
    core.settings.input_rate -= 1
    with pytest.raises(CoreError, match="MODEL_OUTCOME_UNKNOWN"):
        budget.reserve(core.store, core.settings, authorized_job(core),
                               "chat", messages, core.clock())
    assert core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"


def test_multiprocess_concurrency_and_abrupt_exit_preserve_reservations(core, tmp_path):
    import subprocess
    import sys

    from companion import budget

    approved_live(core, tmp_path, concurrency=1)
    job_id = authorized_job(core)
    from tests.support.evaluation_process import process_config

    private_config = tmp_path / "reservation-settings.json"
    process_config(core, private_config)
    command = [
        sys.executable, "-B", "-c",
        "import sys; from tests.process_helpers import reservation_process; "
        "reservation_process(*sys.argv[1:])", str(private_config), core.clock(),
        job_id,
    ]
    processes = [
        subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(4)
    ]
    results = [process.communicate(timeout=40) for process in processes]
    assert all(process.returncode == 23 for process in processes), results
    outputs = [out.strip() for out, _ in results]
    assert sum(value.startswith("model_") for value in outputs) == 1
    assert outputs.count("MODEL_PROVIDER_CONCURRENCY_EXHAUSTED") == 3
    with pytest.raises(CoreError, match="CONCURRENCY"):
        budget.reserve(
            core.store,
            core.settings,
            job_id,
            "chat",
            [{"role": "user", "content": "hello"}],
            core.clock(),
        )
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage")["billed_micro_usd"] > 0


def test_crash_after_fsync_before_db_commit_blocks_old_database(core, tmp_path, monkeypatch):
    from companion import budget
    from companion.model import ModelResponse

    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "hello"}]
    first = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    budget.settle(core.store, core.settings, first, ModelResponse("ok", 30, 2))
    original = budget.paid_budget.journal

    def crash(store, approval, record=None, **kwargs):
        value = original(store, approval, record, **kwargs)
        if record:
            raise SystemExit("synthetic process death after journal fsync")
        return value

    monkeypatch.setattr(budget.paid_budget, "journal", crash)
    with pytest.raises(SystemExit):
        budget.reserve(core.store, core.settings, authorized_job(core),
                               "chat", messages, core.clock())
    monkeypatch.setattr(budget.paid_budget, "journal", original)
    with pytest.raises(CoreError, match="RECONCILIATION"):
        budget.reserve(core.store, core.settings, authorized_job(core),
                               "chat", messages, core.clock())
    assert core.model.calls == []


def test_restored_database_cannot_start_new_paid_window(empty_database, core, tmp_path):
    from cryptography.fernet import Fernet

    from companion import budget
    from companion.backup import backup, restore
    from companion.store import Store

    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "hello"}]
    budget.reserve(core.store, core.settings, authorized_job(core), "chat", messages, core.clock())
    archive, dest, key = tmp_path / "core.enc", empty_database, Fernet.generate_key()
    state = tmp_path / "restored-state"
    backup(core.settings.database, archive, key)
    restore(archive, dest, key, core.settings.erasure_ledger, state_directory=state)
    with Database(dest) as database, closing(Store(database, state)) as store:
        core.settings.paid_model = core.settings.paid_model.model_copy(
            update={"window_id": "new-after-restore"}
        )
        with pytest.raises(CoreError, match="OWNER_AUTHORITY_DISABLED"):
            budget.reserve(store, core.settings, authorized_job(core),
                                   "chat", messages, core.clock())
        with pytest.raises(CoreError, match="RECONCILIATION"):
            budget.paid_budget.journal(store, core.settings.paid_model)
        assert store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"


@pytest.mark.parametrize("failure", [401, 429, 503, "timeout", "missing", "invalid_cache"])
def test_http_failures_retain_upper_reservations_without_automatic_retry(core, tmp_path, failure):
    from companion import budget

    approved_live(core, tmp_path, concurrency=1)
    messages = [{"role": "user", "content": "hello"}]
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        if type(failure) is int:
            return httpx.Response(failure, json={"private": "SYNTHETIC_PROVIDER_SECRET"})
        usage = (
            {}
            if failure == "missing"
            else {"prompt_tokens": 30, "completion_tokens": 2, "prompt_cache_hit_tokens": 40}
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}], "usage": usage}
        )

    async def exercise():
        request = budget.reserve(core.store, core.settings, authorized_job(core),
                               "chat", messages, core.clock())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            try:
                response = await budget.complete(
                    ModelClient(core.settings, client),
                    core.store,
                    core.settings,
                    request,
                    messages,
                    "chat",
                    core.clock,
                )
            except CoreError:
                response = None
            budget.settle(core.store, core.settings, request, response)
            with pytest.raises(CoreError):
                budget.reserve(core.store, core.settings, authorized_job(core),
                                       "chat", messages, core.clock())

    asyncio.run(exercise())
    assert len(calls) == 1
    usage = core.store.one("SELECT state,billed_micro_usd FROM model_usage")
    assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] > 0
    assert "SYNTHETIC_PROVIDER_SECRET" not in str(core.status())


@pytest.mark.parametrize(
    "change,code",
    [
        ("price", "RATE_MISMATCH"),
        ("provider", "PROVIDER_CONTRACT"),
        ("deadline", "APPROVAL_EXPIRED"),
        ("payload", "PAYLOAD_CHANGED"),
    ],
)
def test_after_reserve_changes_fail_before_http(core, tmp_path, change, code):
    from datetime import timedelta

    from companion import budget

    approved_live(core, tmp_path)
    if change == "deadline":
        core.clock.now = core.settings.paid_model.expires_at - timedelta(seconds=1)
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    if change == "price":
        core.settings.input_rate += 1
    elif change == "provider":
        core.settings.model_url = "https://different.invalid"
    elif change == "deadline":
        core.clock.advance(2)
    else:
        messages[0]["content"] = "other"
    calls = []

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: calls.append(req))
        ) as client:
            with pytest.raises(CoreError, match=code):
                await budget.complete(
                    ModelClient(core.settings, client),
                    core.store,
                    core.settings,
                    request,
                    messages,
                    "chat",
                    core.clock,
                )

    asyncio.run(exercise())
    assert calls == []


def test_pre_io_database_rollback_cannot_replay_http_with_newer_journal(
    empty_database, core, tmp_path,
):
    from companion import budget
    from companion.store import Store

    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    snapshot = empty_database
    copy_database(core.settings.database, snapshot, tmp_path)
    calls = []

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: (
                    calls.append(req)
                    or httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
                )
            )
        ) as client:
            await budget.complete(
                ModelClient(core.settings, client),
                core.store,
                core.settings,
                request,
                messages,
                "chat",
                core.clock,
            )
            with (Database(snapshot) as database,
                  closing(Store(database, core.store.state_directory)) as old):
                with pytest.raises(CoreError, match="RECONCILIATION"):
                    await budget.complete(
                        ModelClient(core.settings, client),
                        old,
                        core.settings,
                        request,
                        messages,
                        "chat",
                        core.clock,
                    )

    asyncio.run(exercise())
    assert len(calls) == 1


def test_legacy_unknown_retains_original_evidence_without_blocking_fresh_admission(core, tmp_path):
    from companion import budget

    approved_live(core, tmp_path)
    job_id = authorized_job(core)
    # Historical accounting evidence has no native admission or execution authority.
    core.store.insert("model_usage", usage_id="usage_historical", request_id="model_historical",
                      scenario="chat", provider=core.settings.provider,
                      model=core.settings.model_name, billed_micro_usd=100,
                      state="UNKNOWN", pricing_known=1, occurred_at=core.clock())
    budget.reserve(core.store, core.settings, job_id, "chat",
                   [{"role": "user", "content": "hello"}], core.clock())
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage "
                          "WHERE request_id='model_historical'") == {
        "state": "UNKNOWN", "billed_micro_usd": 100,
    }
    assert len(core.store.rows("SELECT * FROM model_usage")) == 2
    assert core.model.calls == []


def test_provider_invoice_discrepancy_stops_new_calls_and_records_upper_difference(core, tmp_path):
    from companion import budget
    from companion.model import ModelResponse

    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    budget.settle(core.store, core.settings, request, ModelResponse("ok", 30, 2))
    budget.record_provider_discrepancy(core.store, core.settings, request, 8000, "USD", "e" * 64)
    with pytest.raises(CoreError, match="MODEL_OUTCOME_UNKNOWN"):
        budget.reserve(core.store, core.settings, authorized_job(core),
                               "chat", messages, core.clock())
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
        "state": "UNKNOWN",
        "billed_micro_usd": 8000,
    }


def test_window_expiry_during_estimation_keeps_unknown_without_http(core, tmp_path, monkeypatch):
    from datetime import timedelta

    from companion import budget

    approved_live(core, tmp_path)
    core.clock.now = core.settings.paid_model.expires_at - timedelta(seconds=1)
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    original = budget.paid_budget.estimate_input_tokens

    def slow_estimation(*args, **kwargs):
        result = original(*args, **kwargs)
        core.clock.advance(2)
        return result

    monkeypatch.setattr(budget.paid_budget, "estimate_input_tokens", slow_estimation)
    calls = []

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: calls.append(req))
        ) as client:
            with pytest.raises(CoreError, match="APPROVAL_EXPIRED"):
                await budget.complete(
                    ModelClient(core.settings, client),
                    core.store,
                    core.settings,
                    request,
                    messages,
                    "chat",
                    core.clock,
                )

    asyncio.run(exercise())
    assert calls == []
    usage = core.store.one("SELECT state,billed_micro_usd FROM model_usage")
    assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] > 0


@pytest.mark.parametrize("rate", [None, 0, True])
def test_unknown_or_zero_template_rate_never_enables_live(core, tmp_path, rate):
    from pydantic import ValidationError

    from companion import budget

    if rate is None:
        approved_live(core, tmp_path)
        core.settings.input_rate = None
        with pytest.raises(CoreError, match="RATE_MISMATCH"):
            budget.reserve(core.store, core.settings, None, "chat", [], core.clock())
    else:
        with pytest.raises(ValidationError):
            approved_live(core, tmp_path, input_rate=rate)
    assert core.model.calls == []
    assert core.store.rows("SELECT * FROM model_usage") == []


def test_extraction_and_reconciliation_share_one_logical_request_cap(core, tmp_path):
    import json

    from companion.memory_work import checkpoint_state
    from tests.conftest import run
    from tests.sdk_reader_fixture import Provider, answer, worker_for
    from tests.support.native_consumer_entry import environment

    async def exercise():
        async with environment(core, tmp_path, paid_options={"per_request_calls": 1},
            core_options={"extraction_min_events": 1, "extraction_idle_seconds": 0},
        ) as (entry, _):
            source = await entry.admit(1, "我喜欢绿茶，也喜欢红茶")
            extracted = {"memories": [
                {"subject": "user", "kind": "preference", "fact_key": key,
                 "content": text, "evidence_event_id": source["event_id"],
                 "evidence_quote": text}
                for key, text in [("tea_green", "我喜欢绿茶"), ("tea_red", "喜欢红茶")]
            ]}
            model = Provider(answer("合成回复"), answer(json.dumps(extracted, ensure_ascii=False)))
            async with worker_for(core, model) as worker:
                assert await worker.run_once("budget-extraction-chat")
                assert not await worker.extract_once()
            assert len(model.requests) == 2
            assert core.store.rows(
                'SELECT scenario,state FROM model_usage ORDER BY '
                'record_sequence'
            ) == [
                {"scenario": "chat", "state": "SETTLED"},
                {"scenario": "memory_extract", "state": "SETTLED"},
            ]
            assert checkpoint_state(core.store)["batch"]["error"] == (
                "MODEL_REQUEST_BUDGET_EXHAUSTED")
            assert core.store.rows("SELECT * FROM memories") == []
    run(exercise())


@pytest.mark.parametrize("field", ["provider", "model", "journal_path"])
def test_empty_identity_or_proof_location_cannot_form_approval(core, tmp_path, field):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        approved_live(core, tmp_path, **{field: ""})
    assert core.model.calls == []


def test_model_adapter_with_different_settings_cannot_use_another_admission(core, tmp_path):
    from companion import budget

    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    other = core.settings.model_copy(update={"model_url": "https://unapproved.invalid"})
    calls = []

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: (
                    calls.append(req)
                    or httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
                )
            )
        ) as client:
            with pytest.raises(CoreError, match="CONFIGURATION_MISMATCH"):
                await budget.complete(
                    ModelClient(other, client),
                    core.store,
                    core.settings,
                    request,
                    messages,
                    "chat",
                    core.clock,
                )

    asyncio.run(exercise())
    assert calls == []


@pytest.mark.parametrize("stage", ["after_reserve", "between_requests"])
def test_credential_rotation_cannot_reuse_existing_approval_window(core, tmp_path, stage):
    from companion import budget
    from companion.model import ModelResponse

    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    if stage == "between_requests":
        budget.settle(core.store, core.settings, request, ModelResponse("ok", 30, 2))
    core.settings.model_key = SecretStr("different-synthetic-account-key")
    calls = []

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: (
                    calls.append(req)
                    or httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
                )
            )
        ) as client:
            with pytest.raises(CoreError, match="OWNER_AUTHORITY_PAID_POLICY_CHANGED"):
                current = request
                if stage == "between_requests":
                    current = budget.reserve(
                        core.store, core.settings, authorized_job(core),
                                               "chat", messages, core.clock()
                    )
                await budget.complete(
                    ModelClient(core.settings, client),
                    core.store,
                    core.settings,
                    current,
                    messages,
                    "chat",
                    core.clock,
                )

    asyncio.run(exercise())
    assert calls == []


def test_chat_reservation_cannot_pay_for_activity_scenario(core, tmp_path):
    from companion import budget

    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    calls = []

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: (
                    calls.append(req)
                    or httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
                )
            )
        ) as client:
            with pytest.raises(CoreError, match="PAYLOAD_CHANGED"):
                await budget.complete(
                    ModelClient(core.settings, client),
                    core.store,
                    core.settings,
                    request,
                    messages,
                    "activity",
                    core.clock,
                )

    asyncio.run(exercise())
    assert calls == []


def test_stopping_admission_does_not_discard_later_inflight_cost_evidence(core, tmp_path):
    from companion import budget
    from companion.model import ModelResponse

    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "hello"}]
    first = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    second = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    budget.settle(core.store, core.settings, first, ModelResponse("ok", 999999, 2))
    budget.settle(core.store, core.settings, second, ModelResponse("ok", 888888, 2))
    rows = core.store.rows(
        'SELECT state,billed_micro_usd FROM model_usage ORDER BY '
        'record_sequence'
    )
    assert rows == [
        {"state": "SETTLED", "billed_micro_usd": 1000003},
        {"state": "SETTLED", "billed_micro_usd": 888892},
    ]
    budget.record_provider_discrepancy(core.store, core.settings, second, 2000000, "USD", "f" * 64)
    assert (
        core.store.one('SELECT billed_micro_usd FROM model_usage WHERE request_id=%s', (second,))[
            "billed_micro_usd"
        ]
        == 2000000
    )
    with pytest.raises(CoreError, match="MODEL_OUTCOME_UNKNOWN"):
        budget.reserve(core.store, core.settings, authorized_job(core),
                               "chat", messages, core.clock())


@pytest.mark.parametrize("change", ["disabled", "path_changed", "write_failure"])
def test_known_cost_uses_reserved_accounting_custody_after_live_config_changes(
    core, tmp_path, change, monkeypatch
):
    from companion import budget

    approved_live(core, tmp_path)
    original_approval = core.settings.paid_model
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    calls = []

    def provider(req):
        calls.append(req)
        if change == "disabled":
            core.settings.paid_model = None
        elif change == "path_changed":
            core.settings.paid_model = original_approval.model_copy(
                update={"journal_path": str(tmp_path / "different.jsonl")}
            )
        else:
            original = budget.paid_budget.journal

            def fail_write(*args, **kwargs):
                if len(args) > 2:
                    raise CoreError("MODEL_BUDGET_RECONCILIATION_REQUIRED", 503)
                return original(*args, **kwargs)

            monkeypatch.setattr(budget.paid_budget, "journal", fail_write)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 900000, "completion_tokens": 2},
        })

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            await budget.complete(
                ModelClient(core.settings, client),
                core.store,
                core.settings,
                request,
                messages,
                "chat",
                core.clock,
            )

    asyncio.run(exercise())
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
        "state": "UNKNOWN" if change == "write_failure" else "SETTLED",
        "billed_micro_usd": 900004,
    }
    core.settings.paid_model = original_approval
    if change == "write_failure":
        with pytest.raises(CoreError, match="MODEL_OUTCOME_UNKNOWN"):
            budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    else:
        budget.reserve(core.store, core.settings, authorized_job(core),
                       "chat", messages, core.clock())
    assert len(calls) == 1


def test_invoice_survives_disabled_live_approval(core, tmp_path):
    from companion import budget

    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    core.settings.paid_model = None
    budget.record_provider_discrepancy(core.store, core.settings, request, 900004, "USD", "f" * 64)
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
        "state": "UNKNOWN",
        "billed_micro_usd": 900004,
    }


def test_unknown_settlement_cannot_be_rolled_back_to_reserved(empty_database, core, tmp_path):
    from companion import budget
    from companion.store import Store

    approved_live(core, tmp_path)
    messages = [{"role": "user", "content": "hello"}]
    request = budget.reserve(core.store, core.settings, authorized_job(core),
                           "chat", messages, core.clock())
    path = empty_database
    copy_database(core.settings.database, path, tmp_path)
    budget.settle(core.store, core.settings, request)
    calls = []

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: (
                    calls.append(req)
                    or httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
                )
            )
        ) as client:
            with pytest.raises(CoreError, match="RECONCILIATION"):
                await budget.complete(
                    ModelClient(core.settings, client),
                    old,
                    core.settings,
                    request,
                    messages,
                    "chat",
                    core.clock,
                )

    with (Database(path) as database,
          closing(Store(database, core.store.state_directory)) as old):
        asyncio.run(exercise())
        assert calls == []


def test_open_ended_approval_keeps_hash_shape_and_caps(core, tmp_path):
    from datetime import datetime, timedelta

    from companion import paid_budget

    approved_live(core, tmp_path)
    assert {"expires_at", "rates_valid_until"} <= set(paid_budget.snapshot(core.settings))
    approval = approved_live(core, tmp_path, expires_at=None, rates_valid_until=None)
    snapshot = paid_budget.snapshot(core.settings)
    assert "expires_at" not in snapshot and "rates_valid_until" not in snapshot
    later = datetime.fromisoformat("2036-10-01T00:00:00+08:00")
    assert paid_budget.active(approval, later) and paid_budget.rates_valid(approval, later)
    assert not paid_budget.active(approval, approval.starts_at - timedelta(seconds=1))
    assert approval.daily_micro_usd == 200000 and approval.window_calls == 20


@pytest.mark.parametrize("changes", [
    {"rates_valid_until": None},
    {"expires_at": None, "rates_valid_until": "2026-09-01T00:00:00+08:00"},
])
def test_finite_approval_needs_rate_validity_and_open_rates_follow_start(core, tmp_path, changes):
    with pytest.raises(ValueError):
        approved_live(core, tmp_path, **changes)


def test_open_ended_approval_cannot_escape_a_cumulative_scope(core, tmp_path):
    from companion.config import PaidModelApproval
    from tests.support.cumulative_budget import policy

    approved_live(core, tmp_path)
    scope = policy(core)
    values = core.settings.paid_model.model_dump(mode="json")
    values.update(expires_at=None, rates_valid_until=None, cumulative=scope.model_dump(mode="json"))
    with pytest.raises(ValueError, match="cumulative account scope"):
        PaidModelApproval.model_validate(values)
