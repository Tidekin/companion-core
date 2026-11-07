"""A real node claim waiting on another process must not block the HTTP loop."""

import asyncio
import sqlite3
import sys
import threading
from pathlib import Path
from time import perf_counter

import httpx
import pytest

from companion.voice_jobs import VoiceJobs
from companion.voice_protocol import ROUTE_PREFIX
from tests.support.api_store import child_done, event_wait
from tests.support.core_http import real_core_http
from tests.support.voice_jobs import TOKEN, prepared
from tests.test_voice_node_api import claim_body


def test_node_claim_write_wait_keeps_http_responsive(core, monkeypatch, record_property):
    prepared(core, monkeypatch, make_reply=False)
    body = claim_body(core)
    entered = threading.Event()
    original = VoiceJobs.claim
    owner_thread = threading.get_ident()
    connections = []

    def observed(service, *args, **kwargs):
        connections.append((service.store.db, threading.get_ident()))
        entered.set()
        return original(service, *args, **kwargs)

    monkeypatch.setattr(VoiceJobs, "claim", observed)

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", str(Path(__file__).with_name("api_store_lock_worker.py")),
            core.settings.database, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
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
                before = perf_counter()
                await event_wait(entered)
                assert not request.done(), "claim blocked the HTTP loop until its lock timeout"
                health = await asyncio.wait_for(probe.get("/internal/v1/health/live"), 0.75)
                elapsed = perf_counter() - before
                record_property("health_during_node_claim_wait_ms", round(elapsed * 1000, 3))
                assert health.status_code == 200 and elapsed < 0.75
                assert not request.done()
                process.stdin.write(b"release\n")
                await process.stdin.drain()
                response = await asyncio.wait_for(request, 8)
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
    assert all(db is not core.store.db and thread != owner_thread for db, thread in connections)
    assert connections[0][0] is not connections[1][0]
    for connection, _ in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    assert VoiceJobs(core.store, core.settings, core.clock).session("local").last_sequence == (
        body["sequence"]
    )
