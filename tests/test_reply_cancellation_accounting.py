"""Cancellation receipts distinguish unstarted I/O from an abandoned real request."""

import asyncio
import json
from pathlib import Path

import httpx

from companion import budget, owner_authority
from companion.core import Core
from companion.model import ModelClient
from companion.owner_authority_protocol import AuthorityStop
from tests.conftest import run
from tests.native_input_protocol_fixture import admit, environment


def test_supersession_in_last_http_hook_never_calls_old_provider_or_freezes_budget(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with environment(core, tmp_path, monkeypatch) as (client, config, plan, now):
            calls, hooks = [], []

            async def new_input_before_transport(request):
                hooks.append(request)
                if len(hooks) == 1:
                    _, accepted = await admit(client, config, plan, now, 2)
                    assert accepted.status_code == 202, accepted.text

            def provider(request):
                calls.append(request)
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": "最新回复"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                })

            first, accepted = await admit(client, config, plan, now)
            assert accepted.status_code == 202, accepted.text
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider),
                event_hooks={"request": [new_input_before_transport]}) as transport:
                with Core(
                    core.settings, ModelClient(core.settings, transport), core.clock,
                ) as worker:
                    assert worker.store is not core.store
                    assert await worker.run_once("cancel-before-http")
                    assert calls == []
                    usage = core.store.one("SELECT * FROM model_usage")
                    assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] == 0
                    assert core.store.one("SELECT COUNT(*) AS n FROM model_admissions")["n"] == 1
                    assert core.store.one("SELECT 1 FROM audit_log "
                                          "WHERE action='model.not_dispatched'")
                    assert owner_authority.status(
                        core.store, core.settings, core.clock)["state"] == "ACTIVE"
                    assert worker.clock() >= core.store.one(
                        "SELECT MAX(ingested_at) AS latest FROM events")["latest"]
                    assert await worker.run_once("latest-input")
            assert len(calls) == 1, core.store.rows(
                "SELECT kind,state,last_error_code FROM jobs")
            assert core.store.one('SELECT state FROM jobs WHERE event_id=%s',
                                  (first["event_id"],))["state"] == "CANCELLED"
            assert core.store.one("SELECT COUNT(*) AS n FROM model_admissions")["n"] == 2
            assert all(row["state"] == "SETTLED" for row in core.store.rows(
                "SELECT state FROM model_usage"))
            assert core.store.one("SELECT COUNT(*) AS n FROM outbox")["n"] == 1
    run(scenario())


def test_cancelled_worker_loss_expires_reservation_through_independent_journal(
    core, tmp_path, monkeypatch,
):
    async def scenario():
        async with environment(core, tmp_path, monkeypatch) as (client, config, plan, now):
            _, accepted = await admit(client, config, plan, now)
            assert accepted.status_code == 202, accepted.text
            with Core(core.settings, clock=core.clock) as worker:
                job = worker.claim_job("lost-worker")
                messages = worker.context(job)
                request_id = budget.reserve(worker.store, worker.settings, job["job_id"], "chat",
                    messages, worker.clock(), clock=worker.clock)
                _, accepted = await admit(client, config, plan, now, 2)
                assert accepted.status_code == 202, accepted.text
                core.clock.advance(core.settings.timeout_seconds + 11)
                with worker.store.transaction():
                    worker.recover(worker.clock())
                usage = worker.store.one("SELECT state,billed_micro_usd FROM model_usage")
                assert usage["state"] == "UNKNOWN" and usage["billed_micro_usd"] > 0
                journal = await asyncio.to_thread(
                    Path(core.settings.paid_model.journal_path).read_text, "utf-8",
                )
                records = [json.loads(line) for line in journal.splitlines()]
                last = [row for row in records if row.get("request_id") == request_id][-1]
                assert last["state"] == "UNKNOWN"
                assert last["minimum_micro_usd"] == usage["billed_micro_usd"]
                stopped = owner_authority.stop(worker.store, worker.settings, worker.clock,
                    AuthorityStop(operation_id="stop-abandoned-request", authority=plan.authority))
                assert stopped["state"] == "STOPPED" and stopped["admission_fenced"]
                assert not stopped["quiescence_proven"]
    run(scenario())
