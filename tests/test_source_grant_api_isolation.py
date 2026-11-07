"""Source-grant writes preserve custody and owner authority off the API loop."""

import asyncio
import sys
import threading
from contextlib import closing
from pathlib import Path
from time import perf_counter

import httpx
import pytest
from cryptography.fernet import Fernet

from companion import api_store, console_api
from companion.backup import backup, restore
from companion.source_grants import grant_tombstone
from companion.store import Store
from tests.support.console_reset_isolation import configure as console_configure
from tests.support.console_reset_isolation import login
from tests.support.core_http import real_core_http
from tests.support.exploration import configure
from tests.support.source_grants import accepted, authorization, derived_records, lease, prepare


@pytest.mark.parametrize("channel,action", [
    ("internal", "register"), ("internal", "revoke"), ("internal", "forget"),
    ("console", "revoke"), ("console", "forget"),
])
def test_source_grant_write_wait_keeps_health_and_original_semantics(
    core, tmp_path, monkeypatch, record_property, channel, action,
):
    if action == "register":
        configure(core)
        grant, root, pending = authorization(core), None, None
    else:
        grant = prepare(core)
        root = accepted(core, grant)
        derived_records(core, root)
        pending = lease(core, grant, key="pending-read")
    console_configure(core)
    key, archive, restored = Fernet.generate_key(), tmp_path / "before.enc", tmp_path / "restore.db"
    if action == "forget":
        backup(core.settings.database, archive, key)
    entered, opened, closed = threading.Event(), [], []

    class ObservedStore(Store):
        def __init__(self, path):
            super().__init__(path)
            opened.append((self, threading.get_ident()))
            self.db.set_trace_callback(
                lambda sql: entered.set() if sql == "BEGIN IMMEDIATE" else None
            )

        def close(self):
            closed.append((self, threading.get_ident()))
            super().close()

    async def scenario():
        async with (
            real_core_http(core) as port,
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client,
            httpx.AsyncClient(base_url=client.base_url, trust_env=False) as probe,
        ):
            if channel == "console":
                headers = {**await login(client), "Idempotency-Key": "source-grant-action"}
                path, body = "/v1/console/source-grants/" + grant.grant_id, {"action": action}
            else:
                headers = {"Authorization": "Bearer "
                           + core.settings.control_token.get_secret_value()}
                path = "/internal/v2/source-grants"
                body = {"adapter_id": "heybox", **grant.snapshot()} if action == "register" else {}
                if action != "register":
                    path += "/" + grant.grant_id + "/" + action
            monkeypatch.setattr(api_store, "Store", ObservedStore)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", str(Path(__file__).with_name("api_store_lock_worker.py")),
                core.settings.database, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            task = None
            try:
                assert (await asyncio.wait_for(process.stdout.readline(), 5)).strip() == b"LOCKED"
                rejected = await client.post(path, json=body, headers={"Authorization": "Bearer "
                    + core.settings.bridge_token.get_secret_value()})
                assert rejected.status_code == 401 and not opened
                assert (await probe.get("/internal/v1/health/live")).status_code == 200
                task = asyncio.create_task(client.post(path, json=body, headers=headers))
                assert await asyncio.to_thread(entered.wait, 3)
                before = perf_counter()
                health = await asyncio.wait_for(probe.get("/internal/v1/health/live"), 0.75)
                elapsed = perf_counter() - before
                record_property("health_during_grant_write_wait_ms", round(elapsed * 1000, 3))
                assert health.status_code == 200 and elapsed < 0.75 and not task.done()
                core.clock.advance(2)
                process.stdin.write(b"release\n")
                await process.stdin.drain()
                response = await asyncio.wait_for(task, 8)
                assert response.status_code == (201 if action == "register" else 200), response.text
                ledger = core.ledger.path.read_bytes()
                again = await client.post(path, json=body, headers=headers)
                assert again.status_code == response.status_code, again.text
                if action == "register":
                    assert not response.json()["duplicate"] and again.json()["duplicate"]
                    assert core.store.one("SELECT created_at FROM source_grants")["created_at"] == (
                        core.clock())
                else:
                    assert again.json() == response.json()
                assert core.ledger.path.read_bytes() == ledger
                await asyncio.wait_for(process.communicate(), 5)
                assert process.returncode == 0
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                if task:
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(opened) == len(closed) == 2 and opened == closed
    assert all(store is not core.store and thread != threading.get_ident()
               for store, thread in opened)
    if pending:
        row = core.store.one('SELECT * FROM exploration_requests WHERE request_id=%s',
                             (pending["request_id"],))
        assert row["state"] == "CANCELLED" and row["lease_token"] is None
        assert row["reserved_requests"] == pending["max_requests"] and row["used_requests"] is None
        state = core.store.one('SELECT redacted FROM events WHERE event_id=%s', (root,))
        assert state["redacted"] == (1 if action == "forget" else 0)
    if action == "forget":
        assert core.store.one("SELECT status FROM memories WHERE memory_id='mem_grant_fixture'")[
            "status"] == "FORGOTTEN"
        restore(archive, restored, key, core.settings.erasure_ledger)
        with closing(Store(str(restored))) as store:
            state = store.one("SELECT redacted FROM events WHERE event_id=?", (root,))
            assert state["redacted"] == 1
            assert store.one("SELECT value FROM runtime_meta WHERE key=?",
                             (grant_tombstone(grant.grant_id),))


def test_source_grant_registration_rechecks_expiry_after_write_lock(core, monkeypatch):
    configure(core)
    console_configure(core)
    grant = authorization(core)
    entered, release = threading.Event(), threading.Event()

    class GatedStore(Store):
        def __init__(self, path):
            super().__init__(path)
            self.db.set_trace_callback(lambda sql: (
                entered.set(), release.wait(5)
            ) if sql == "BEGIN IMMEDIATE" else None)

    monkeypatch.setattr(api_store, "Store", GatedStore)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.control_token.get_secret_value()},
        ) as client:
            task = asyncio.create_task(client.post("/internal/v2/source-grants",
                json={"adapter_id": "heybox", **grant.snapshot()}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                core.clock.advance(3601)
                release.set()
                response = await asyncio.wait_for(task, 5)
                assert response.status_code == 403 and response.json()["error"] == (
                    "SOURCE_GRANT_EXPIRED")
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert not core.store.rows("SELECT * FROM source_grants")
    assert not core.store.rows("SELECT * FROM source_grant_revisions")


@pytest.mark.parametrize("action", ["revoke", "forget"])
def test_console_grant_action_rechecks_logout_before_native_effect(core, monkeypatch, action):
    grant = prepare(core)
    root = accepted(core, grant)
    console_configure(core)
    original = console_api._operation
    before = core.store.one("SELECT * FROM source_grants")
    ledger = core.ledger.path.read_bytes()

    def operation(context, request, key, body, apply, **kwargs):
        def revoke_session():
            with closing(Store(core.settings.database)) as store:
                with store.transaction():
                    store.execute("DELETE FROM console_sessions")
            return apply()
        return original(context, request, key, body, revoke_session, **kwargs)

    monkeypatch.setattr(console_api, "_operation", operation)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as client:
            headers = {**await login(client), "Idempotency-Key": "revoked-grant-control"}
            response = await client.post("/v1/console/source-grants/" + grant.grant_id,
                                         json={"action": action}, headers=headers)
            assert response.status_code == 401, response.text

    asyncio.run(scenario())
    assert core.store.one("SELECT * FROM source_grants") == before
    assert core.store.one('SELECT redacted FROM events WHERE event_id=%s', (root,))["redacted"] == 0
    assert core.ledger.path.read_bytes() == ledger
