"""Administrative erasures hold the privacy lock on an independent API session."""

import asyncio
import threading

import httpx
import pytest
from pydantic import SecretStr

from companion import api_store
from companion.store import Store
from tests.api_store_lock_worker import start
from tests.support.api_store import child_done, event_wait
from tests.support.core_http import real_core_http
from tests.support.observation_capture import enable_observations, observation


@pytest.mark.parametrize(("path", "credential", "status"), [
    ("/internal/v2/observations/obs_test_1/forget", "control_token", "REDACTED"),
    ("/internal/v1/diary/2026-10-18/forget", "diary_admin_token", "DELETED"),
])
def test_waiting_erasure_leaves_health_live_and_rechecks_token(
    core, monkeypatch, path, credential, status,
):
    enable_observations(core)
    core.observations.append("reader", observation(core))
    original = SecretStr("synthetic-initial-credential" * 2)
    replacement = SecretStr("synthetic-rotated-credential" * 2)
    setattr(core.settings, credential, original)
    ledger = core.ledger.path.read_bytes()
    entered = threading.Event()

    class ObservedStore(Store):
        def _control_lock(self, exclusive):
            entered.set()
            return super()._control_lock(exclusive)

    monkeypatch.setattr(api_store, "Store", ObservedStore)

    async def scenario():
        async with (
            real_core_http(core) as port,
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client,
        ):
            process = await start(core)
            pending = None
            try:
                assert (await asyncio.wait_for(process.stdout.readline(), 5)).strip() == b"LOCKED"
                pending = asyncio.create_task(client.post(path, headers={
                    "Authorization": "Bearer " + original.get_secret_value()}))
                await event_wait(entered)
                live = await asyncio.wait_for(client.get("/internal/v1/health/live"), 2)
                assert live.status_code == 200 and not pending.done()
                setattr(core.settings, credential, replacement)
            finally:
                process.stdin.write(b"release\n")
                await process.stdin.drain()
                await child_done(process)
                if pending is not None:
                    response = await pending
            assert response.status_code == 401
            assert core.ledger.path.read_bytes() == ledger
            completed = await client.post(path, headers={
                "Authorization": "Bearer " + replacement.get_secret_value()})
            assert completed.status_code == 200
            assert completed.json()["status"] == status
            assert len(core.ledger.read()) == 2

    asyncio.run(scenario())

