"""Native inbox admission must remain atomic while other HTTP requests can progress."""

import asyncio
import sqlite3
import threading
from contextlib import closing

import httpx
import pytest

from companion import api_store
from companion.store import Store
from tests.conftest import event
from tests.support.core_http import real_core_http


def test_inbox_waiting_for_writer_keeps_health_live_and_deduplicates(core, monkeypatch):
    entered = threading.Event()

    class TracedStore(Store):
        def __init__(self, path):
            super().__init__(path)
            self.db.set_trace_callback(
                lambda sql: entered.set() if sql.startswith("BEGIN IMMEDIATE") else None
            )

    monkeypatch.setattr(api_store, "Store", TracedStore)
    body = event(core).model_dump(mode="json")
    headers = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value(),
               "X-Idempotency-Key": "inbox-lock-replay"}

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as client:
            with closing(sqlite3.connect(core.settings.database)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                requests = [asyncio.create_task(client.post(
                    "/internal/v1/inbox/events", json=body, headers=headers,
                )) for _ in range(2)]
                try:
                    assert await asyncio.to_thread(entered.wait, 3)
                    live = await asyncio.wait_for(client.get("/internal/v1/health/live"), 1)
                    assert live.status_code == 200 and not any(r.done() for r in requests)
                    core.clock.advance(2)
                finally:
                    writer.rollback()
                    responses = await asyncio.gather(*requests)
            assert [r.status_code for r in responses] == [202, 202]
            first, second = sorted((r.json() for r in responses), key=lambda v: v["duplicate"])
            assert first["duplicate"] is False and second == {**first, "duplicate": True}
            job = core.store.one("SELECT created_at FROM jobs WHERE job_id=?", (first["job_id"],))
            assert job["created_at"] == core.clock()

    asyncio.run(scenario())


@pytest.mark.parametrize("route", ["owner-authority", "live-text"])
def test_recall_waiting_for_writer_keeps_http_live_and_commits_once(
    core, tmp_path, monkeypatch, route, record_property,
):
    from time import perf_counter

    from tests.support.handoff_recall_recovery import approve_only
    from tests.support.input_protocol import environment, notice
    from tests.support.owner_input import captured, inbound
    from tests.support.owner_recall import receipt

    entered = threading.Event()

    class TracedStore(Store):
        def __init__(self, path):
            super().__init__(path)
            self.db.set_trace_callback(
                lambda sql: entered.set() if sql.startswith("BEGIN IMMEDIATE") else None
            )

    async def verify(body):
        monkeypatch.setattr(api_store, "Store", TracedStore)
        path = f"/internal/v1/{route}/inputs/recall"
        headers = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, headers=headers,
        ) as client, httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as probe:
            with closing(sqlite3.connect(core.settings.database)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                request = asyncio.create_task(client.post(path, json=body))
                try:
                    assert await asyncio.to_thread(entered.wait, 3)
                    before = perf_counter()
                    live = await asyncio.wait_for(probe.get("/internal/v1/health/live"), 1)
                    record_property("health_ms", round(1000 * (perf_counter() - before), 3))
                    assert live.status_code == 200 and not request.done()
                finally:
                    writer.rollback()
                    result = await request
            assert result.status_code == 200 and result.json()["state"] == "RECALLED"
            repeated = await client.post(path, json=body)
            assert repeated.status_code == 200 and repeated.json()["duplicate"]

    async def scenario():
        if route == "live-text":
            async with environment(core, tmp_path, monkeypatch) as (client, config, plan, now):
                await approve_only(client, config, plan, now)
                await verify(notice(config, plan, now))
        else:
            from companion import owner_input

            capture, key = captured(core, tmp_path)
            owner_input.approve(core.store, core.settings, core.clock, capture)
            core.ingest(inbound(core, capture), "recall-with-lock")
            await verify(receipt(core, capture, key).model_dump(mode="json"))

    asyncio.run(scenario())
