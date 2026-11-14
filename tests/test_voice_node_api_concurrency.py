"""A real node claim waiting on another process must not block the HTTP loop."""

import asyncio
import threading
from time import perf_counter

import httpx
import pytest

from companion.store import Store
from companion.voice_jobs import VoiceJobs
from companion.voice_protocol import ROUTE_PREFIX
from tests.api_store_lock_worker import start as start_lock_worker
from tests.support.api_store import child_done, event_wait
from tests.support.core_http import real_core_http
from tests.support.voice_jobs import TOKEN, prepared
from tests.support.voice_node_api import claim_body


def test_node_claim_write_wait_keeps_http_responsive(core, monkeypatch, record_property):
    prepared(core, monkeypatch, make_reply=False)
    body = claim_body(core)
    entered = threading.Event()
    original = VoiceJobs.claim
    owner_thread = threading.get_ident()
    connections, closed = [], {}
    close = Store.close

    def observe_close(store):
        closed[store] = threading.get_ident()
        return close(store)

    monkeypatch.setattr(Store, "close", observe_close)

    def observed(service, *args, **kwargs):
        connections.append((service.store, service.store.backend_pid, threading.get_ident()))
        entered.set()
        return original(service, *args, **kwargs)

    monkeypatch.setattr(VoiceJobs, "claim", observed)

    async def scenario():
        process = await start_lock_worker(core)
        requests = []
        try:
            assert (await asyncio.wait_for(process.stdout.readline(), 5)).strip() == b"LOCKED"
            async with real_core_http(core) as port, httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10,
            ) as client, httpx.AsyncClient(base_url=client.base_url, trust_env=False) as probe:
                assert (await probe.get("/internal/v1/health/live")).status_code == 200
                request = asyncio.create_task(client.post(
                    ROUTE_PREFIX + "/claim", json=body,
                    headers={"Authorization": "Bearer " + TOKEN,
                             "Idempotency-Key": f"voice-claim:{body['incarnation']}:"
                                                f"{body['sequence']}"},
                ))
                requests.append(request)
                try:
                    await event_wait(entered)
                    assert not request.done(), "claim blocked the HTTP loop until its lock timeout"
                    before = perf_counter()
                    # Health must complete while the independent process still holds the lock.
                    health = await asyncio.wait_for(probe.get("/internal/v1/health/live"), 5)
                    elapsed = perf_counter() - before
                    record_property("health_during_node_claim_wait_ms", round(elapsed * 1000, 3))
                    assert health.status_code == 200 and not request.done()
                finally:
                    # Release and join before HTTP shutdown, including on an assertion failure.
                    process.stdin.write(b"release\n")
                    await process.stdin.drain()
                    await asyncio.gather(request, return_exceptions=True)
                response = request.result()
                assert response.status_code == 200, response.text
                assert response.headers["cache-control"] == "no-store"
                # The exact native claim can be reconciled without advancing twice.
                replay = await client.post(
                    ROUTE_PREFIX + "/claim", json=body,
                    headers={"Authorization": "Bearer " + TOKEN,
                             "Idempotency-Key": f"voice-claim:{body['incarnation']}:"
                                                f"{body['sequence']}"},
                )
                assert replay.status_code == 200 and replay.json() == response.json()
            await child_done(process)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await asyncio.gather(*requests, return_exceptions=True)

    asyncio.run(scenario())
    assert len(connections) == 2
    assert all(pid != core.store.backend_pid and thread != owner_thread
               for _, pid, thread in connections)
    # A sequential replay acquires a fresh Store lease; the pool may reuse its backend.
    assert connections[0][0] is not connections[1][0]
    for connection, _, thread in connections:
        assert closed[connection] == thread
        with pytest.raises(RuntimeError, match="POSTGRES_SESSION_CLOSED"):
            connection.execute("SELECT 1")
    assert VoiceJobs(core.store, core.settings, core.clock).session("local").last_sequence == (
        body["sequence"]
    )
