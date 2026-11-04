"""Console reset keeps HTTP responsive and rechecks the queued owner session."""

import asyncio
import sqlite3
import sys
import threading
from pathlib import Path
from time import perf_counter

import httpx
import pytest
from pydantic import SecretStr

from companion import api_store, console_api
from companion.console_api import COOKIE
from companion.privacy import epoch
from companion.store import Store
from tests.support.core_http import real_core_http

ORIGIN = "https://console.example.test"
PASSWORD = "synthetic-console-reset-" * 2
PATH = "/v1/console/affect/reset"


def configure(core):
    core.settings.console.enabled = True
    core.settings.console.public_origin = ORIGIN
    core.settings.control_token = SecretStr(PASSWORD)


async def login(client):
    response = await client.post("/v1/console/session", json={"password": PASSWORD},
                                 headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"],
            "Cookie": COOKIE + "=" + response.cookies[COOKIE],
            "Idempotency-Key": "console-reset-isolation"}


def test_console_reset_wait_keeps_health_responsive_and_retry_idempotent(
    core, monkeypatch, record_property,
):
    configure(core)
    entered = threading.Event()
    opened, closed = [], []
    initial_epoch = epoch(core.store)

    class ObservedStore(Store):
        def __init__(self, path):
            super().__init__(path)
            opened.append((self, threading.get_ident()))
            entered.set()

        def close(self):
            closed.append((self, threading.get_ident()))
            super().close()

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as client:
            headers = await login(client)
            monkeypatch.setattr(api_store, "Store", ObservedStore)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", str(Path(__file__).with_name("api_store_lock_worker.py")),
                core.settings.database, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            pending = None
            try:
                assert (await asyncio.wait_for(process.stdout.readline(), 5)).strip() == b"LOCKED"
                for rejected, status in [({**headers, "Origin": "https://other.example"}, 403),
                                         ({**headers, "X-CSRF-Token": "wrong"}, 403),
                                         ({**headers, "Cookie": ""}, 401)]:
                    denied = await client.post(PATH, json={"confirm": True}, headers=rejected)
                    assert denied.status_code == status
                assert not opened
                pending = asyncio.create_task(client.post(PATH, json={"confirm": True},
                                                          headers=headers))
                assert await asyncio.to_thread(entered.wait, 3)
                before = perf_counter()
                health = await asyncio.wait_for(client.get("/internal/v1/health/live"), 0.75)
                elapsed = perf_counter() - before
                record_property("health_during_console_reset_wait_ms", round(elapsed * 1000, 3))
                assert health.status_code == 200 and elapsed < 0.75 and not pending.done()
                core.clock.advance(1)
                process.stdin.write(b"release\n")
                await process.stdin.drain()
                response = await asyncio.wait_for(pending, 8)
                assert response.status_code == 200, response.text
                assert response.json() == {"state": "RESET"}
                assert response.headers["Cache-Control"] == "no-store"
                retry = await client.post(PATH, json={"confirm": True}, headers=headers)
                assert retry.status_code == 200 and retry.json() == response.json()
                await asyncio.wait_for(process.communicate(), 5)
                assert process.returncode == 0
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                if pending:
                    await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(scenario())
    assert len(opened) == len(closed) == 2
    assert {id(s): t for s, t in opened} == {id(s): t for s, t in closed}
    for store, thread in opened:
        assert store is not core.store and thread != threading.get_ident()
        with pytest.raises(sqlite3.ProgrammingError):
            store.one("SELECT 1")
    assert epoch(core.store) == initial_epoch + 1
    records = [row for row in core.ledger.read() if row.get("target_type") == "affect_reset"]
    assert len(records) == 1 and records[0]["at"] == core.clock()
    assert core.store.one("SELECT state FROM console_operations")["state"] == "COMPLETE"


@pytest.mark.parametrize("change", ["expiry", "logout", "credential", "disabled"])
def test_console_reset_rechecks_session_after_queue_wait(core, monkeypatch, change):
    configure(core)
    entered, release = threading.Event(), threading.Event()
    initial_epoch = epoch(core.store)
    initial_ledger = core.ledger.path.read_bytes()

    class GatedStore(Store):
        def __init__(self, path):
            super().__init__(path)
            entered.set()
            assert release.wait(5)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as client:
            headers = await login(client)
            monkeypatch.setattr(api_store, "Store", GatedStore)
            pending = asyncio.create_task(client.post(PATH, json={"confirm": True},
                                                      headers=headers))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                if change == "expiry":
                    core.clock.advance(core.settings.console.session_seconds + 1)
                elif change == "logout":
                    core.store.execute("DELETE FROM console_sessions")
                elif change == "credential":
                    core.settings.control_token = SecretStr("replacement-control-" * 3)
                else:
                    core.settings.console.enabled = False
                release.set()
                response = await asyncio.wait_for(pending, 5)
                assert response.status_code == (404 if change == "disabled" else 401)
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(scenario())
    assert not core.store.rows("SELECT * FROM console_operations")
    assert epoch(core.store) == initial_epoch
    assert core.ledger.path.read_bytes() == initial_ledger


def test_console_reset_rechecks_logout_between_receipt_and_effect(core, monkeypatch):
    configure(core)
    original = console_api._operation
    initial_epoch = epoch(core.store)
    initial_ledger = core.ledger.path.read_bytes()

    def operation(context, request, key, body, apply, **kwargs):
        def revoke_before_apply():
            # A separate connection commits revocation after STARTED was recorded.
            store = Store(core.settings.database)
            try:
                with store.transaction():
                    store.execute("DELETE FROM console_sessions")
            finally:
                store.close()
            return apply()

        return original(context, request, key, body, revoke_before_apply, **kwargs)

    monkeypatch.setattr(console_api, "_operation", operation)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as client:
            headers = await login(client)
            response = await client.post(PATH, json={"confirm": True}, headers=headers)
            assert response.status_code == 401, response.text

    asyncio.run(scenario())
    assert core.store.one("SELECT state FROM console_operations")["state"] == "STARTED"
    assert epoch(core.store) == initial_epoch
    assert core.ledger.path.read_bytes() == initial_ledger
