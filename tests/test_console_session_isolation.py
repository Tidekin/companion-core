"""Owner session writes stay responsive and decide against post-wait state."""

import asyncio
import sys
import threading
from pathlib import Path
from time import perf_counter

import httpx
import pytest
from pydantic import SecretStr

from companion import api_store
from companion.store import Store, after
from tests.support.core_http import real_core_http
from tests.test_console_reset_isolation import ORIGIN, PASSWORD, configure, login

PATH = "/v1/console/session"


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_session_write_wait_keeps_health_responsive(core, monkeypatch, record_property, method):
    configure(core)
    entered, opened, closed = threading.Event(), [], []

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
            headers = await login(client) if method == "DELETE" else {"Origin": ORIGIN}
            monkeypatch.setattr(api_store, "Store", ObservedStore)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", str(Path(__file__).with_name("api_store_lock_worker.py")),
                core.settings.database, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            pending = None
            try:
                assert (await asyncio.wait_for(process.stdout.readline(), 5)).strip() == b"LOCKED"
                options = {"json": {"password": PASSWORD}} if method == "POST" else {}
                pending = asyncio.create_task(client.request(
                    method, PATH, headers=headers, **options))
                assert await asyncio.to_thread(entered.wait, 3)
                before = perf_counter()
                health = await asyncio.wait_for(client.get("/internal/v1/health/live"), 0.75)
                elapsed = perf_counter() - before
                record_property("health_during_session_write_wait_ms", round(elapsed * 1000, 3))
                assert health.status_code == 200 and elapsed < 0.75 and not pending.done()
                core.clock.advance(2)
                process.stdin.write(b"release\n")
                await process.stdin.drain()
                response = await asyncio.wait_for(pending, 8)
                assert response.status_code == 200, response.text
                cookie = response.headers["set-cookie"].lower()
                assert all(flag in cookie for flag in ("secure", "httponly", "samesite=strict"))
                if method == "POST":
                    expires = after(core.clock(), core.settings.console.session_seconds)
                    assert response.json()["expires_at"] == expires
                    row = core.store.one("SELECT created_at,expires_at FROM console_sessions")
                    assert row == {"created_at": core.clock(), "expires_at": expires}
                else:
                    assert response.json() == {"state": "SIGNED_OUT"}
                    assert not core.store.rows("SELECT * FROM console_sessions")
                await asyncio.wait_for(process.communicate(), 5)
                assert process.returncode == 0
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                if pending:
                    await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(scenario())
    assert len(opened) == len(closed) == 1
    assert opened == closed
    assert opened[0][0] is not core.store and opened[0][1] != threading.get_ident()


@pytest.mark.parametrize("method,change,expected", [
    ("POST", "credential", 401), ("POST", "origin", 403), ("POST", "disabled", 404),
    ("DELETE", "expiry", 401), ("DELETE", "credential", 401),
    ("DELETE", "origin", 403), ("DELETE", "csrf", 403), ("DELETE", "disabled", 404),
])
def test_session_write_rechecks_authority_after_queue(core, monkeypatch, method, change, expected):
    configure(core)
    entered, release = threading.Event(), threading.Event()

    class GatedStore(Store):
        def __init__(self, path):
            super().__init__(path)
            entered.set()
            assert release.wait(5)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as client:
            headers = await login(client) if method == "DELETE" else {"Origin": ORIGIN}
            before = core.store.rows("SELECT * FROM console_sessions")
            monkeypatch.setattr(api_store, "Store", GatedStore)
            options = {"json": {"password": PASSWORD}} if method == "POST" else {}
            pending = asyncio.create_task(client.request(method, PATH, headers=headers, **options))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                if change == "expiry":
                    core.clock.advance(core.settings.console.session_seconds + 1)
                elif change == "credential":
                    core.settings.control_token = SecretStr("replacement-password-" * 3)
                elif change == "origin":
                    core.settings.console.public_origin = "https://changed.example.test"
                elif change == "csrf":
                    core.store.execute("UPDATE console_sessions SET csrf_token='changed'")
                    before = core.store.rows("SELECT * FROM console_sessions")
                else:
                    core.settings.console.enabled = False
                release.set()
                response = await asyncio.wait_for(pending, 5)
                assert response.status_code == expected, response.text
                assert "set-cookie" not in response.headers
                assert core.store.rows("SELECT * FROM console_sessions") == before
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(scenario())


def test_concurrent_login_rate_and_session_limits_remain_atomic(core):
    configure(core)
    core.settings.console.max_sessions = 2

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, headers={"Origin": ORIGIN},
        ) as client:
            failures = await asyncio.gather(*[
                client.post(PATH, json={"password": "incorrect"}) for _ in range(6)
            ])
            assert sorted(response.status_code for response in failures) == [401] * 5 + [429]
            assert core.store.one("SELECT attempts FROM console_login_attempts")["attempts"] == 5
            assert not core.store.rows("SELECT * FROM console_sessions")
            assert (await client.post(PATH, json={"password": PASSWORD})).status_code == 429
            core.clock.advance(301)
            successes = await asyncio.gather(*[
                client.post(PATH, json={"password": PASSWORD}) for _ in range(3)
            ])
            assert all(response.status_code == 200 for response in successes)
            assert len(core.store.rows("SELECT * FROM console_sessions")) == 2
            assert not core.store.rows("SELECT * FROM console_login_attempts")

    asyncio.run(scenario())
