"""Native inbox admission must remain atomic while other HTTP requests can progress."""

import asyncio
import sqlite3
import threading
from contextlib import closing

import httpx

from companion import api_store
from companion.store import Store
from tests.conftest import event
from tests.test_onebot_capture import real_core_http


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
