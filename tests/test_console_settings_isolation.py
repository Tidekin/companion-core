"""Versioned configuration and budget changes retain their native receipts and liabilities."""

import asyncio
import sys
import threading
from pathlib import Path
from time import perf_counter

import httpx
import pytest
from pydantic import SecretStr

from companion import api_store, cloud_budget
from companion.store import Store
from tests.support.core_http import real_core_http
from tests.test_cloud_budget import charge
from tests.test_console_reset_isolation import configure, login


def request(core, target):
    configure(core)
    policy = cloud_budget.policy(core.store, core.settings, core.clock())
    return ({"expected_version": 0, "changes": {"max_output_tokens": 1024}}
            if target == "configuration" else
            {"expected_version": policy["version"], "changes": {"daily_micro_cny": 18_000_000}})


@pytest.mark.parametrize("target", ["configuration", "cloud-budget"])
def test_settings_wait_keeps_health_versions_and_unknown_cost(core, monkeypatch, record_property,
                                                            target):
    body = request(core, target)
    charge(core, "synthetic-unknown", 80)
    core.store.execute("UPDATE model_usage SET state='UNKNOWN' "
                       "WHERE request_id='synthetic-unknown'")
    cloud_budget.summary(core.store, core.settings, core.clock())
    usage, costs = (core.store.rows("SELECT * FROM " + table)
                    for table in ("model_usage", "cloud_cost_snapshots"))
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
            headers = {**await login(client), "Idempotency-Key": "settings-isolation"}
            monkeypatch.setattr(api_store, "Store", ObservedStore)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", str(Path(__file__).with_name("api_store_lock_worker.py")),
                core.settings.database, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            task = None
            try:
                assert (await asyncio.wait_for(process.stdout.readline(), 5)).strip() == b"LOCKED"
                path = "/v1/console/" + target
                task = asyncio.create_task(client.put(path, json=body, headers=headers))
                assert await asyncio.to_thread(entered.wait, 3)
                before = perf_counter()
                health = await asyncio.wait_for(client.get("/internal/v1/health/live"), 0.75)
                elapsed = perf_counter() - before
                record_property("health_during_settings_write_wait_ms", round(elapsed * 1000, 3))
                assert health.status_code == 200 and elapsed < 0.75 and not task.done()
                core.clock.advance(2)
                process.stdin.write(b"release\n")
                await process.stdin.drain()
                changed = await asyncio.wait_for(task, 8)
                assert changed.status_code == 200, changed.text
                repeated = await client.put(path, json=body, headers=headers)
                assert repeated.status_code == 200 and repeated.json() == changed.json()
                stale = await client.put(path, json=body,
                    headers={**headers, "Idempotency-Key": "stale-settings-version"})
                assert stale.status_code == 409, stale.text
                table = "operator_configuration" if target == "configuration" else (
                    "cloud_budget_changes")
                rows = core.store.rows("SELECT created_at FROM " + table)
                assert rows == [{"created_at": core.clock()}]
                await asyncio.wait_for(process.communicate(), 5)
                assert process.returncode == 0
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                if task:
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(opened) == len(closed) == 3 and opened == closed
    assert all(store is not core.store and thread != threading.get_ident()
               for store, thread in opened)
    assert core.store.rows("SELECT * FROM model_usage") == usage
    assert core.store.rows("SELECT * FROM cloud_cost_snapshots") == costs
    assert core.settings.max_output_tokens == 768


@pytest.mark.parametrize("target", ["configuration", "cloud-budget"])
@pytest.mark.parametrize("change", ["expiry", "logout", "credential"])
def test_settings_change_rechecks_session_after_queue(core, monkeypatch, target, change):
    body = request(core, target)
    before = {table: core.store.rows("SELECT * FROM " + table) for table in (
        "operator_configuration", "cloud_budget_changes", "cloud_budget_policies")}
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
            headers = {**await login(client), "Idempotency-Key": "cancelled-settings"}
            monkeypatch.setattr(api_store, "Store", GatedStore)
            task = asyncio.create_task(client.put(
                "/v1/console/" + target, json=body, headers=headers))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                if change == "expiry":
                    core.clock.advance(core.settings.console.session_seconds + 1)
                elif change == "logout":
                    core.store.execute("DELETE FROM console_sessions")
                else:
                    core.settings.control_token = SecretStr("replacement-owner-" * 3)
                release.set()
                response = await asyncio.wait_for(task, 5)
                assert response.status_code == 401, response.text
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert {table: core.store.rows("SELECT * FROM " + table) for table in before} == before
