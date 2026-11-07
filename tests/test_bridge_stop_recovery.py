"""Bridge evidence failure must not prevent authenticated Core revocation."""

from pathlib import Path

import httpx
import pytest

from companion import budget, live_text, paid_budget
from companion.config import ObservationAdapterConfig
from companion.contracts import CoreError
from companion.core import Core
from companion.model import ModelClient, ModelResponse
from companion.store import after, encode, uid
from companion_onebot_bridge import continuous
from companion_onebot_bridge.network import private_http_client
from tests.conftest import run
from tests.historical_input_retirement import historical_input
from tests.support.onebot_capture import source_window
from tests.support.private_companion import companion_options


def damage(window, fault):
    if fault == "missing":
        window.plan.journal.unlink()
    elif fault == "head":
        with window.db:
            window.db.execute("UPDATE test_window SET journal_head=?", ("f" * 64,))
    else:
        window.plan.journal.write_bytes(b"{broken-json\n")


def pending_history(core, job):
    """Original queued liabilities; no retired worker or admission writer is executed."""
    now = core.clock()
    core.store.execute("UPDATE jobs SET state='SUCCEEDED',last_error_code=NULL WHERE job_id=%s",
                       (job["job_id"],))
    core.store.insert("outbox", message_id=uid("msg"), origin_event_id=job["event_id"],
        conversation_id=job["conversation_id"], action_type="qq.private.reply",
        content_json=encode({"text": "保留的待发回复"}),
        content_sha256=paid_budget.sha({"text": "保留的待发回复"}), delivery_key="archived-reply",
        state="PENDING", created_at=now, updated_at=now)
    requests = []
    for state in ("RUNNING", "QUEUED"):
        request = uid("read")
        core.store.insert("exploration_requests", request_id=request, request_key=request,
            adapter_id="bili", operation="discover", url="",
            lease_token=uid("read_lease") if state == "RUNNING" else None,
            lease_until=after(now, 60) if state == "RUNNING" else None,
            started_at=now if state == "RUNNING" else None,
            state=state, captured_privacy_epoch=0, created_at=now, updated_at=now,
            reserved_bytes=100, used_bytes=0)
        requests.append(request)
    request = uid("model")
    snapshot = paid_budget.snapshot(core.settings)
    approval = core.settings.paid_model
    contract = paid_budget.sha(snapshot)
    core.store.insert("model_budget_windows", window_id=approval.window_id,
                      contract_sha256=contract)
    core.store.insert("model_usage", usage_id=uid("usage"), request_id=request, job_id=None,
        scenario="memory_extract", provider=core.settings.provider, model=core.settings.model_name,
        input_tokens=100, output_tokens=0, billed_micro_usd=200, is_estimate=1, pricing_known=1,
        state="RESERVED", occurred_at=now)
    core.store.insert("model_admissions", request_id=request, logical_request_id=request,
        attempt_id=uid("attempt"), attempt_number=1, window_id=approval.window_id,
        contract_sha256=contract, payload_sha256=paid_budget.sha({"historical_request": request}),
        snapshot_json=encode(snapshot), reserved_micro_usd=200, input_bound=100, output_bound=20,
        journal_path=str(Path(approval.journal_path).resolve()),
        journal_id=core.settings.live_text_window.budget_journal_id,
        lease_token=uid("model_lease"), lease_until=after(now, 60))
    paid_budget.journal(core.store, approval, dict(kind="reserve", request_id=request,
        state="RESERVED", minimum_micro_usd=200, contract_sha256=contract))
    return *requests, request


@pytest.mark.parametrize("fault", ["broken", "missing", "head"])
def test_journal_failure_revokes_historical_custody_through_api_and_independent_worker(
    core, tmp_path, fault,
):
    calls = []

    def forbidden(request):
        calls.append(request)
        pytest.fail("Historical recovery must not invoke an external provider")

    async def scenario():
        options = companion_options(core)
        options["link_scope"] = "owner_links_v1"
        options["core_options"].update(
            content_enabled=True, observation_enabled=True,
            exploration_daily_requests=8, exploration_monthly_requests=30,
            exploration_daily_bytes=4_194_304,
            observation_adapters={"bili": ObservationAdapterConfig(
                source="bilibili", token="synthetic-reader-token-12345",
            )},
        )
        async with historical_input(core, tmp_path, failure="cancelled", stopped=False,
                                    scope_options=options, rich_input_scope="references_v1") as (
            window, client, job,
        ):
            running, queued, reserved = pending_history(core, job)
            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as model, (
                private_http_client(window.plan.config.onebot_http_url,
                    window.plan.config.onebot_token, transport=httpx.MockTransport(forbidden))
            ) as protocol:
                with Core(core.settings, ModelClient(core.settings, model), core.clock) as worker:
                    assert worker.store is not core.store
                    damage(window, fault)
                    before = (window.plan.journal.read_bytes()
                              if window.plan.journal.exists() else None)
                    with pytest.raises((ValueError, FileNotFoundError)):
                        await continuous.run(window, client, protocol)
                    assert window.core_stop_ack is True
                    assert window.stop_path.read_bytes() == b"OWNER_STOP\n"
                    assert live_text.stopped(worker.store, window.plan.sha256)
                    assert (window.plan.journal.read_bytes()
                            if window.plan.journal.exists() else None) == before
                    assert worker.store.one("SELECT state FROM outbox")["state"] == "CANCELLED"
                    assert worker.store.one(
                        'SELECT state FROM exploration_requests WHERE request_id=%s', (running,),
                    )["state"] == "UNKNOWN"
                    assert worker.store.one(
                        'SELECT state FROM exploration_requests WHERE request_id=%s', (queued,),
                    )["state"] == "CANCELLED"
                    assert worker.exploration.lease("bili") is None
                    assert not await worker.run_once("after-stop")
                    with pytest.raises(CoreError):
                        budget.reserve(worker.store, worker.settings, None, "memory_extract",
                            [{"role": "user", "content": "synthetic denied work"}],
                            worker.clock(), clock=worker.clock)
                    # A supplied late known receipt settles the retained reservation.
                    budget.settle(worker.store, worker.settings, reserved,
                                  ModelResponse("", 10, 1, 0))
                    assert worker.store.one(
                        'SELECT state FROM model_usage WHERE request_id=%s', (reserved,),
                    )["state"] == "SETTLED"
                    assert calls == []

    run(scenario())


@pytest.mark.parametrize("entry", ["stop_live", "stop_core"])
def test_direct_revocation_does_not_require_readable_journal(core, tmp_path, entry):
    async def source(ws):
        await ws.wait_closed()

    async def scenario():
        async with source_window(core, tmp_path, source, live=True, approved=True,
                                 scope_options={"automatic_reply": True}) as (
            window, client, _, _, _,
        ):
            damage(window, "broken")
            with pytest.raises(ValueError):
                await getattr(window, entry)(client)
            assert window.core_stop_ack is True
            assert live_text.stopped(core.store, window.plan.sha256)

    run(scenario())


def test_damaged_journal_and_unreachable_core_report_unconfirmed(core, tmp_path):
    async def source(ws):
        await ws.wait_closed()

    def disconnected(request):
        raise httpx.ConnectError("synthetic private failure")

    async def scenario():
        async with source_window(core, tmp_path, source, live=True, approved=True,
                                 scope_options={"automatic_reply": True}) as (
            window, client, protocol, _, _,
        ):
            damage(window, "broken")
            client._transport = httpx.MockTransport(disconnected)
            with pytest.raises(ValueError, match="LIVE_TEXT_CORE_STOP_UNCONFIRMED"):
                await continuous.run(window, client, protocol)
            assert window.core_stop_ack is False
            assert window.stop_path.read_bytes() == b"OWNER_STOP\n"
            assert not live_text.stopped(core.store, window.plan.sha256)
            assert window.plan.journal.read_bytes() == b"{broken-json\n"

    run(scenario())


def test_durable_stop_ack_survives_core_outage_and_mutated_plan_cannot_retarget(core, tmp_path):
    async def source(ws):
        await ws.wait_closed()

    def disconnected(request):
        pytest.fail("A verified durable STOP receipt must not require another HTTP call")

    async def scenario():
        async with source_window(core, tmp_path, source, live=True, approved=True,
                                 scope_options={"automatic_reply": True}) as (
            window, client, _, _, _,
        ):
            original = window.plan.sha256
            window.plan.sha256 = "f" * 64
            with pytest.raises(ValueError, match="WINDOW_IMMUTABLE_OR_MISSING"):
                await window.stop_core(client)
            assert live_text.stopped(core.store, original) and window.core_stop_ack
            window.plan.sha256 = original
            result = await window.stop_live(client)
            assert result["core_stop_confirmed"] is True
            client._transport = httpx.MockTransport(disconnected)
            await window.stop_core(client)
            assert window.core_stop_ack is True

    run(scenario())
