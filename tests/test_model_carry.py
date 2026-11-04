"""Closure changes physical admission only; financial and source custody stay native."""

import asyncio
import json

import httpx
import pytest

from companion import (
    budget,
    model_carry,
    owner_authority,
    owner_input,
    owner_input_state,
    paid_budget,
)
from companion.contracts import CoreError
from companion.model import ModelClient
from companion_delivery_protocol import digest
from companion_delivery_protocol import model_carry as wire
from tests.model_carry_support import MESSAGES, executors, migrated, observer, stranded
from tests.owner_model_fixtures import authorized_job
from tests.support.owner_authority import activate


def test_explicit_carry_preserves_unknown_nonterminal_and_original_caps(core, tmp_path):
    case = stranded(core, tmp_path)
    financial = core.store.one("SELECT * FROM model_usage WHERE request_id=?", (case.request_id,))
    state = owner_input_state.read(core.store, core.settings, case.capture)
    assert state["unsettled_model"] and not state["terminal"] and not state["unsettled_work"]
    receipt = model_carry.prepare(
        core.store, core.settings, case.request, case.verified, core.clock()
    )
    assert wire.carried(state, case.capture.model_dump(mode="json"), receipt) == state
    assert receipt["retained_micro_usd"] == financial["billed_micro_usd"]
    assert (
        model_carry.prepare(core.store, core.settings, case.request, case.verified, core.clock())
        == receipt
    )
    assert (
        model_carry.read(
            core.store,
            core.settings,
            case.active["authority"],
            receipt["carry_sha256"],
            digest(case.request["successor_policy"]),
        )
        == receipt
    )
    successor, _ = activate(core, case.policy, revision=2, operation="activate-carry-test")
    assert successor["authority"]["grant_id"] == case.active["authority"]["grant_id"]
    assert (
        successor["authority"]["restore_generation"]
        == case.active["authority"]["restore_generation"]
    )
    assert successor["authority"]["revision"] == 3
    assert (
        core.store.one("SELECT * FROM model_usage WHERE request_id=?", (case.request_id,))
        == financial
    )
    assert owner_input_state.read(core.store, core.settings, case.capture)["unsettled_model"]
    with pytest.raises(CoreError):
        owner_input.approve(core.store, core.settings, core.clock, case.capture)
    with pytest.raises(CoreError):
        owner_authority.current(
            core.store,
            core.settings,
            core.clock,
            authority=case.active["authority"],
            purpose="chat",
        )


@pytest.mark.parametrize(
    "change", ["amount", "expiry", "concurrency", "manifest", "head", "authority", "witness"]
)
def test_carry_rejects_changed_approval_or_incomplete_evidence(core, tmp_path, change):
    case = stranded(core, tmp_path)
    if change == "amount":
        core.settings.paid_model = core.settings.paid_model.model_copy(update={"window_calls": 100})
        case.request["successor_policy"]["paid_contract_sha256"] = paid_budget.sha(
            paid_budget.snapshot(core.settings)
        )
    elif change == "expiry":
        from datetime import timedelta

        core.settings.paid_model = core.settings.paid_model.model_copy(
            update={"expires_at": core.settings.paid_model.expires_at + timedelta(seconds=1)}
        )
    elif change == "concurrency":
        core.settings.paid_model = core.settings.paid_model.model_copy(update={"concurrency": 2})
    elif change in {"manifest", "head"}:
        case.request["manifest_sha256" if change == "manifest" else "expected_head_sha256"] = (
            "0" * 64
        )
    elif change == "authority":
        case.request["authority"] = case.request["authority"] | {"revision": 2}
    else:
        case.verified = {"closed": True}
    before = core.store.rows("SELECT * FROM model_usage")
    with pytest.raises(CoreError):
        model_carry.prepare(core.store, core.settings, case.request, case.verified, core.clock())
    assert core.store.rows("SELECT * FROM model_usage") == before
    assert owner_authority._latest(core.store)["state"] == "STOPPED"


def test_new_window_counts_original_window_attempts(core, tmp_path):
    case = migrated(core, tmp_path, window_calls=1)
    job = authorized_job(core, number=2)
    with pytest.raises(CoreError, match="MODEL_WINDOW_BUDGET_EXHAUSTED"):
        budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())
    with core.store.transaction():
        view = paid_budget.journal(core.store, core.settings.paid_model, allow_settlement=True)
    assert model_carry.scope_windows(view, "successor-window-1") == [
        "offline-window-1",
        "successor-window-1",
    ]
    assert case.request_id in view["model_closures"]


def test_duplicate_request_finally_cannot_release_original_active_transport(core, tmp_path):
    case = migrated(core, tmp_path)
    job = authorized_job(core, number=2)
    request_id = budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())
    original = core.store.one(
        "SELECT billed_micro_usd FROM model_usage WHERE request_id=?", (case.request_id,)
    )["billed_micro_usd"]

    async def exercise():
        entered, finish = asyncio.Event(), asyncio.Event()

        async def provider(request):
            entered.set()
            await finish.wait()
            raise httpx.ReadTimeout("synthetic finished transport", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            model = ModelClient(core.settings, client)
            first = asyncio.create_task(
                budget.complete(
                    model, core.store, core.settings, request_id, MESSAGES, "chat", core.clock
                )
            )
            await entered.wait()
            with pytest.raises(CoreError, match="MODEL_REQUEST_ALREADY_STARTED"):
                await budget.complete(
                    model, core.store, core.settings, request_id, MESSAGES, "chat", core.clock
                )
            with core.store.transaction():
                view = paid_budget.journal(core.store, core.settings.paid_model)
            assert request_id not in view["model_closures"]
            # A second logical reservation is blocked while A is physically active.
            with core.store.transaction():
                from datetime import datetime

                day = datetime.fromisoformat(core.clock()).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                month = day.replace(day=1)
                with pytest.raises(CoreError, match="MODEL_PROVIDER_CONCURRENCY_EXHAUSTED"):
                    budget._admit(
                        core.store,
                        core.settings,
                        core.settings.paid_model,
                        "independent",
                        1,
                        core.clock(),
                        day,
                        month,
                    )
            finish.set()
            with pytest.raises(CoreError, match="MODEL_RETRYABLE"):
                await first
            with core.store.transaction():
                view = paid_budget.journal(core.store, core.settings.paid_model)
            assert request_id in view["model_closures"]

    asyncio.run(exercise())
    assert core.store.one(
        "SELECT billed_micro_usd,state FROM model_usage WHERE request_id=?", (case.request_id,)
    ) == {"billed_micro_usd": original, "state": "UNKNOWN"}


@pytest.mark.parametrize("failure", ["running", "missing", "restart"])
def test_controller_observes_every_real_executor(failure):
    from companion.model_carry_controller import verify

    items = executors("2026-10-19T04:00:00+00:00")
    native = observer(items)

    def observe(*args):
        output = native(*args)
        if args[0] == "inspect":
            value = json.loads(output)
            if failure == "running":
                value[0]["State"]["Running"] = True
            elif failure == "restart":
                value[0]["HostConfig"]["RestartPolicy"]["Name"] = "unless-stopped"
            elif len(args) > 1:
                value.append({**value[0], "Id": "e" * 64})
            return json.dumps(value)
        return output

    with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_NOT_QUIESCENT"):
        verify(items, observe=observe)


def test_later_closed_unknown_does_not_permanently_block_another_independent_input(core, tmp_path):
    migrated(core, tmp_path)

    async def exercise():
        def timeout(request):
            raise httpx.ReadTimeout("synthetic locally terminated attempt", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            for number in (2, 3):
                job = authorized_job(core, number=number)
                request_id = budget.reserve(
                    core.store, core.settings, job, "chat", MESSAGES, core.clock()
                )
                with pytest.raises(CoreError, match="MODEL_RETRYABLE"):
                    await budget.complete(
                        ModelClient(core.settings, client),
                        core.store,
                        core.settings,
                        request_id,
                        MESSAGES,
                        "chat",
                        core.clock,
                    )
                with pytest.raises(CoreError, match="MODEL_OUTCOME_UNKNOWN"):
                    budget.reserve(core.store, core.settings, job, "chat", MESSAGES, core.clock())

    asyncio.run(exercise())
    assert len(core.store.rows("SELECT request_id FROM model_usage WHERE state='UNKNOWN'")) == 3
    with core.store.transaction():
        view = paid_budget.journal(core.store, core.settings.paid_model)
    assert len(view["model_closures"]) == 3
