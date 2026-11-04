"""Reader lease and pre-I/O validation retain authority after API queue waits."""

import asyncio
import sys
import threading
from pathlib import Path
from time import perf_counter

import httpx
import pytest

from companion import api_store
from companion.store import Store, after
from tests.conftest import ingest
from tests.support.core_http import real_core_http
from tests.test_exploration import configure
from tests.test_reading_service import configure as configure_reading
from tests.test_reply_revision import accept
from tests.test_source_grants import URL, prepare


def pending(core, version):
    if version == 3:
        configure_reading(core)
        accept(core, text="请阅读 https://articles.example/story")
        adapter = "web"
    else:
        configure(core)
        adapter = "bili"
        with core.store.transaction():
            core.exploration.create(adapter, "discover", "api-lease-isolation")
    return adapter


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("operation", ["lease", "validate"])
def test_reader_admission_wait_keeps_health_and_uses_current_time(
    core, monkeypatch, record_property, version, operation,
):
    adapter = pending(core, version)
    service = core.reading if version == 3 else core.exploration
    item = service.lease(adapter) if operation == "validate" else None
    path = f"/internal/v{version}/readers/{adapter}/" + (
        item["request_id"] + "/validate" if item else "lease")
    body = {"lease_token": item["lease_token"]} if item else {}
    entered, opened, closed = threading.Event(), [], []

    class ObservedStore(Store):
        def __init__(self, path):
            super().__init__(path)
            opened.append((self, threading.get_ident()))
            entered.set()

        def close(self):
            closed.append((self, threading.get_ident()))
            super().close()

    monkeypatch.setattr(api_store, "Store", ObservedStore)

    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", str(Path(__file__).with_name("api_store_lock_worker.py")),
            core.settings.database, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        task = None
        try:
            assert (await asyncio.wait_for(process.stdout.readline(), 5)).strip() == b"LOCKED"
            async with real_core_http(core) as port, httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False,
            ) as client:
                denied = await client.post(path, json=body)
                assert denied.status_code == 401 and not opened
                token = core.settings.observation_adapters[adapter].token.get_secret_value()
                task = asyncio.create_task(client.post(path, json=body,
                    headers={"Authorization": "Bearer " + token}))
                assert await asyncio.to_thread(entered.wait, 3)
                before = perf_counter()
                health = await asyncio.wait_for(client.get("/internal/v1/health/live"), 0.75)
                elapsed = perf_counter() - before
                record_property("health_during_reader_admission_wait_ms", round(elapsed * 1000, 3))
                assert health.status_code == 200 and elapsed < 0.75 and not task.done()
                core.clock.advance(40)
                process.stdin.write(b"release\n")
                await process.stdin.drain()
                response = await asyncio.wait_for(task, 8)
                if operation == "validate":
                    assert response.status_code == 409, response.text
                    assert response.json()["error"] == "STALE_READER_LEASE"
                else:
                    assert response.status_code == 200, response.text
                    assert response.json()["item"]
                    row = core.store.one("SELECT started_at,lease_until FROM exploration_requests")
                    assert row == {"started_at": core.clock(),
                                   "lease_until": after(core.clock(), 30)}
            await asyncio.wait_for(process.communicate(), 5)
            assert process.returncode == 0
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if task:
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(opened) == len(closed) == 1 and opened == closed
    assert opened[0][0] is not core.store and opened[0][1] != threading.get_ident()


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("operation", ["lease", "validate"])
@pytest.mark.parametrize("change", ["revoke", "expiry"])
def test_reader_queue_rechecks_source_grant(core, monkeypatch, version, operation, change):
    grant = prepare(core)
    core.settings.content_enabled = True
    source = ingest(core, text="synthetic owner source")
    with core.store.transaction():
        request_id = core.exploration.create("heybox", "read", "grant-queue", URL,
                                             source_event_ids=[source["event_id"]])
    service = core.reading if version == 3 else core.exploration
    item = service.lease("heybox", grant) if operation == "validate" else None
    body = {"source_grant": grant.snapshot()}
    if item:
        body["lease_token"] = item["lease_token"]
    path = f"/internal/v{version}/readers/heybox/" + (
        request_id + "/validate" if item else "lease")
    entered, release = threading.Event(), threading.Event()

    class GatedStore(Store):
        def __init__(self, path):
            super().__init__(path)
            entered.set()
            assert release.wait(5)

    monkeypatch.setattr(api_store, "Store", GatedStore)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer "
                     + core.settings.observation_adapters["heybox"].token.get_secret_value()},
        ) as client:
            task = asyncio.create_task(client.post(path, json=body))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                if change == "revoke":
                    core.observations.grants.revoke(grant.grant_id)
                else:
                    core.clock.advance(3601)
                before = core.store.one("SELECT * FROM exploration_requests WHERE request_id=?",
                                        (request_id,))
                release.set()
                response = await asyncio.wait_for(task, 5)
                assert response.status_code in {403, 409}, response.text
                assert response.json()["error"] in {
                    "SOURCE_GRANT_REVOKED", "SOURCE_GRANT_EXPIRED", "STALE_READER_LEASE",
                }
                assert core.store.one("SELECT * FROM exploration_requests WHERE request_id=?",
                                      (request_id,)) == before
                assert not core.store.rows("SELECT * FROM observations")
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_http_reader_lease_preserves_explicit_video_capability_and_media_limits(core):
    from companion.bili_video_protocol import CAPABILITY
    from tests.test_bili_video import metadata

    module, item_id, source = metadata(core)
    original = core.store.one("SELECT read_request_id FROM content_items WHERE item_id=?",
                              (item_id,))
    child = module.schedule(item_id, source)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer "
                     + core.settings.observation_adapters["bili"].token.get_secret_value()},
        ) as client:
            path = "/internal/v3/readers/bili"
            without = await client.post(path + "/lease", json={})
            assert without.status_code == 200 and without.json() == {"item": None}
            response = await client.post(path + "/lease", json={"capabilities": [CAPABILITY]})
            assert response.status_code == 200, response.text
            item = response.json()["item"]
            assert item["request_id"] == child and item["max_items"] == 1
            assert item["max_bytes"] == 33 * 1024 * 1024 and item["max_requests"] == 8
            native = core.store.one("SELECT * FROM exploration_requests WHERE request_id=?",
                                    (child,))
            assert native["reserved_bytes"] == item["max_bytes"]
            assert native["reserved_requests"] == item["max_requests"]
            assert native["lease_until"] == item["media"]["deadline"]
            checked = await client.post(path + "/" + child + "/validate",
                                        json={"lease_token": item["lease_token"]})
            assert checked.status_code == 200 and checked.json() == {"valid": True}

    asyncio.run(scenario())
    assert core.store.one("SELECT read_request_id FROM content_items WHERE item_id=?",
                          (item_id,)) == original
    assert not core.store.rows("SELECT * FROM model_usage")
