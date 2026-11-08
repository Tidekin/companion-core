"""Real HTTP Reader receipts and native status execute outside the API loop."""

import asyncio
import threading
from time import perf_counter

import httpx
import pytest

from companion import api_store, ops_status
from companion.content import ContentService
from companion.core import Core
from companion.exploration import ReadItem, ReadResult
from companion.reading import service_for_store
from companion.retrieval import SemanticIndex
from companion.store import Store
from tests.api_store_lock_worker import start as start_lock_worker
from tests.postgres_fixture import logical_state
from tests.support.core_http import real_core_http
from tests.support.exploration import URL, configure
from tests.support.reading_service import queued


def forbid_bootstrap(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("API operation bootstrapped unrelated services")

    monkeypatch.setattr(Core, "__init__", forbidden)
    monkeypatch.setattr(ContentService, "__init__", forbidden)
    monkeypatch.setattr(SemanticIndex, "warmup", forbidden)


def test_reader_composition_has_no_startup_writes_or_shared_connection(core, monkeypatch):
    forbid_bootstrap(monkeypatch)
    store = Store(core.database, core.settings.state_directory)
    try:
        before = logical_state(core.store)
        with store.read_snapshot():
            service = service_for_store(store, core.settings, core.clock)
        assert logical_state(core.store) == before
        for native in (service, service.video, service.exploration,
                       service.exploration.observations, service.exploration.goals,
                       service.exploration.social):
            assert native.store is store and native.store is not core.store
    finally:
        store.close()


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("expired", [False, True])
def test_reader_receipt_lock_wait_keeps_health_and_rechecks_lease(
    core, monkeypatch, record_property, version, expired,
):
    if version == 3:
        adapter, item, body = queued(core)
    else:
        configure(core)
        adapter = "bili"
        with core.store.transaction():
            core.exploration.create(adapter, "discover", "api-isolation")
        item = core.exploration.lease(adapter)
        body = ReadResult(lease_token=item["lease_token"], status="SUCCESS", used_bytes=1200,
                          items=[ReadItem(title="synthetic item", url=URL)]).model_dump(mode="json")
    token = core.settings.observation_adapters[adapter].token.get_secret_value()
    forbid_bootstrap(monkeypatch)
    entered, connections = threading.Event(), []

    class ObservedStore(Store):
        def __init__(self, database, state_directory):
            super().__init__(database, state_directory)
            connections.append((self, threading.get_ident()))

        def _control_lock(self, exclusive):
            entered.set()
            return super()._control_lock(exclusive)

    monkeypatch.setattr(api_store, "Store", ObservedStore)

    async def scenario():
        process = await start_lock_worker(core)
        pending = None
        try:
            assert (await asyncio.wait_for(process.stdout.readline(), 5)).strip() == b"LOCKED"
            async with (
                real_core_http(core) as port,
                httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client,
                httpx.AsyncClient(base_url=client.base_url, trust_env=False) as probe,
            ):
                path = f"/internal/v{version}/readers/{adapter}/{item['request_id']}/result"
                denied = await client.post(path, json=body)
                assert denied.status_code == 401 and not connections
                assert (await probe.get("/internal/v1/health/live")).status_code == 200
                headers = {"Authorization": "Bearer " + token}
                pending = asyncio.create_task(client.post(path, json=body, headers=headers))
                assert await asyncio.to_thread(entered.wait, 3)
                before = perf_counter()
                health = await asyncio.wait_for(probe.get("/internal/v1/health/live"), 0.75)
                elapsed = perf_counter() - before
                record_property("health_during_reader_write_wait_ms", round(elapsed * 1000, 3))
                assert health.status_code == 200 and elapsed < 0.75 and not pending.done()
                if expired:
                    core.clock.advance(3600)
                process.stdin.write(b"release\n")
                await process.stdin.drain()
                response = await asyncio.wait_for(pending, 8)
                if expired and version == 2:
                    assert response.status_code == 409, response.text
                    assert response.json()["error"] == "STALE_READER_LEASE"
                else:
                    assert response.status_code == 200, response.text
                    assert response.json()["state"] == ("CANCELLED" if expired else "DONE")
                    repeated = await client.post(path, json=body, headers=headers)
                    assert repeated.status_code == 200 and repeated.json()["duplicate"]
                assert len(core.store.rows("SELECT * FROM observations")) == (0 if expired else 1)
            await asyncio.wait_for(process.communicate(), 5)
            assert process.returncode == 0
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if pending:
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(scenario())
    assert all(store is not core.store and thread != threading.get_ident()
               for store, thread in connections)
    assert not core.model.calls


@pytest.mark.parametrize("summary", [False, True])
def test_status_native_snapshot_wait_keeps_health_responsive(core, monkeypatch, summary):
    before_status = core.status()
    before_rows = logical_state(core.store)
    before_ledger = core.ledger.path.read_bytes()
    forbid_bootstrap(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    threads = []
    snapshot = ops_status.snapshot

    def delayed(settings):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return snapshot(settings)

    monkeypatch.setattr(ops_status, "snapshot", delayed)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
        ) as client, httpx.AsyncClient(base_url=client.base_url, trust_env=False) as probe:
            assert (await probe.get("/internal/v1/health/live")).status_code == 200
            pending = asyncio.create_task(client.get("/internal/v1/ops/status",
                params={"summary_only": str(summary).lower()}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                health = await asyncio.wait_for(probe.get("/internal/v1/health/live"), 0.75)
                assert health.status_code == 200 and not pending.done()
                release.set()
                response = await asyncio.wait_for(pending, 5)
                assert response.status_code == 200, response.text
                value = response.json()
                assert response.headers["Cache-Control"] == "no-store"
                assert value["observation"]["worker_health_proven"] is False
                if summary:
                    assert set(value) == {"operations", "observation"}
                else:
                    for key in before_status.keys() - {"operations"}:
                        assert value[key] == before_status[key]
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(scenario())
    assert threads and all(t != threading.get_ident() for t in threads)
    assert logical_state(core.store) == before_rows
    assert core.ledger.path.read_bytes() == before_ledger
