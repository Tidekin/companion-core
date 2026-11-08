"""Real HTTP/native transactions retain authority and receipts off the API loop."""

import asyncio
import json
import pickle
import sys
import threading
from contextlib import closing
from pathlib import Path
from time import perf_counter

import anyio
import httpx
import psycopg
import pytest
from pydantic import SecretStr

from companion import api, api_store, owner_authority, owner_input, postgres
from companion.privacy import epoch
from companion.store import Store
from tests.api_store_lock_worker import start as start_lock_worker
from tests.support.api_store import PREFIX, child_done, event_wait, headers
from tests.support.core_http import real_core_http
from tests.support.owner_authority import activate
from tests.support.owner_input import captured, inbound


@pytest.mark.parametrize("action", ["inputs/approve", "stop"])
def test_http_lock_wait_keeps_health_responsive_and_connections_independent(
    core, tmp_path, monkeypatch, record_property, action,
):
    capture, _ = captured(core, tmp_path)
    auth = headers(core)
    body = capture.model_dump(mode="json")
    if action == "stop":
        core.settings.control_token = SecretStr("synthetic-owner-control-token")
        auth = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
        body = {"operation_id": "test-contended-stop", "authority": capture.authority}
    opened, closed, entered = [], [], threading.Event()
    owner_thread = threading.get_ident()

    class ObservedStore(Store):
        def __init__(self, database, state_directory):
            super().__init__(database, state_directory)
            opened.append((self, self.backend_pid, threading.get_ident()))
            if len(opened) == 2:
                entered.set()

        def close(self):
            closed.append((self, threading.get_ident()))
            super().close()

    monkeypatch.setattr(api_store, "Store", ObservedStore)

    async def scenario():
        process = await start_lock_worker(core)
        requests = []
        try:
            ready = await asyncio.wait_for(process.stdout.readline(), 5)
            assert ready.rstrip(b"\r\n") == b"LOCKED"
            async with real_core_http(core) as port, httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, headers=headers(core),
            ) as client, httpx.AsyncClient(base_url=client.base_url, trust_env=False) as probe:
                assert (await probe.get(PREFIX + "/health/live")).status_code == 200
                requests = [asyncio.create_task(client.post(
                    PREFIX + "/owner-authority/" + action, json=body, headers=auth,
                )) for _ in range(2)]
                await event_wait(entered)
                assert not any(request.done() for request in requests)
                before = perf_counter()
                response = await asyncio.wait_for(probe.get(PREFIX + "/health/live"), 0.75)
                elapsed = perf_counter() - before
                record_property("health_during_write_wait_ms", round(elapsed * 1000, 3))
                assert response.status_code == 200
                assert elapsed < 0.75
                assert not any(request.done() for request in requests)
                process.stdin.write(b"release\n")
                await process.stdin.drain()
                responses = await asyncio.wait_for(asyncio.gather(*requests), 8)
                assert all(response.status_code == 200 for response in responses), [
                    (r.status_code, r.text[:200]) for r in responses if r.status_code != 200
                ]
                if action == "stop":
                    assert responses[0].json() == responses[1].json()
                    assert responses[0].json()["state"] == "STOPPED"
                else:
                    assert sorted(response.json()["duplicate"] for response in responses) == [
                        False, True]
            await child_done(process)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await asyncio.gather(*requests, return_exceptions=True)
    asyncio.run(scenario())
    assert len(opened) == len(closed) == 2
    assert opened[0][1] != opened[1][1]
    assert all(pid != core.store.backend_pid and thread != owner_thread
               for _, pid, thread in opened)
    assert {id(store): thread for store, _, thread in opened} == {
        id(store): thread for store, thread in closed}
    for store, _, _ in opened:
        with pytest.raises(RuntimeError, match="POSTGRES_SESSION_CLOSED"):
            store.execute("SELECT 1")
    if action == "inputs/approve":
        assert core.store.one('SELECT COUNT(*) n FROM runtime_meta WHERE key=%s',
                              (owner_input.INPUT + capture.event_id,))["n"] == 1
    else:
        records = map(json.loads, Path(core.settings.paid_model.journal_path).read_text(
            encoding="utf-8",
        ).splitlines())
        stopped, = [r for r in records if r["kind"] == owner_authority.RECORD_KIND
                    and r["state"] == "STOPPED"]
        assert stopped["operation_id"] == body["operation_id"]
        assert core.store.rows(
            "SELECT operation_id, record_sha256 FROM owner_authority_revisions "
            "WHERE state='STOPPED'"
        ) == [{"operation_id": stopped["operation_id"], "record_sha256": stopped["sha256"]}]


def test_affect_reset_wait_keeps_health_responsive_and_duplicate_reset_atomic(
    core, monkeypatch, record_property,
):
    core.settings.control_token = SecretStr("synthetic-affect-control-" * 2)
    opened, closed, entered = [], [], threading.Event()
    owner_thread, previous_epoch = threading.get_ident(), epoch(core.store)

    class ObservedStore(Store):
        def __init__(self, database, state_directory):
            super().__init__(database, state_directory)
            opened.append((self, threading.get_ident()))
            if len(opened) == 2:
                entered.set()

        def close(self):
            closed.append((self, threading.get_ident()))
            super().close()

    monkeypatch.setattr(api_store, "Store", ObservedStore)

    async def scenario():
        process = await start_lock_worker(core)
        requests = []
        try:
            assert (await asyncio.wait_for(process.stdout.readline(), 5)).strip() == b"LOCKED"
            async with real_core_http(core) as port, httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False,
            ) as client, httpx.AsyncClient(base_url=client.base_url, trust_env=False) as probe:
                assert (await probe.get(PREFIX + "/health/live")).status_code == 200
                path = "/internal/v2/affect/reset"
                body = {"request_id": "concurrent-affect-reset"}
                denied = await client.post(path, json=body, headers=headers(core))
                assert denied.status_code == 401 and not opened
                control = {"Authorization": "Bearer "
                           + core.settings.control_token.get_secret_value()}
                invalid = await client.post(path, json={"request_id": ""}, headers=control)
                assert invalid.status_code == 400 and not opened
                requests = [asyncio.create_task(client.post(path, json=body, headers=control))
                            for _ in range(2)]
                await event_wait(entered)
                before = perf_counter()
                health = await asyncio.wait_for(probe.get(PREFIX + "/health/live"), 0.75)
                elapsed = perf_counter() - before
                record_property("health_during_affect_write_wait_ms", round(elapsed * 1000, 3))
                assert health.status_code == 200 and elapsed < 0.75
                assert not any(request.done() for request in requests)
                core.clock.advance(1)
                process.stdin.write(b"release\n")
                await process.stdin.drain()
                responses = await asyncio.wait_for(asyncio.gather(*requests), 8)
                assert all(response.status_code == 200 for response in responses), [
                    (r.status_code, r.text[:200]) for r in responses if r.status_code != 200
                ]
                assert all(response.headers["Cache-Control"] == "no-store"
                           for response in responses)
                assert responses[0].json() == responses[1].json() == {
                    "state": "RESET", "reset_at": core.clock(),
                }
            await child_done(process)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await asyncio.gather(*requests, return_exceptions=True)

    asyncio.run(scenario())
    assert len(opened) == len(closed) == 2
    assert {id(store): thread for store, thread in opened} == {
        id(store): thread for store, thread in closed}
    for store, thread in opened:
        assert store is not core.store and thread != owner_thread
        with pytest.raises(RuntimeError, match="POSTGRES_SESSION_CLOSED"):
            store.one("SELECT 1")
    assert epoch(core.store) == previous_epoch + 1
    assert len([row for row in core.ledger.read() if row.get("target_type") == "affect_reset"]) == 1


@pytest.mark.parametrize(("route", "function", "payload"), [
    ("/deliveries/lease", "lease_deliveries", {"worker_id": "clock-check"}),
    ("/deliveries/missing/begin-send", "begin_send", {"lease_token": "missing-token"}),
    ("/deliveries/missing/result", "delivery_result",
     {"lease_token": "missing-token", "result": "DELIVERY_UNCERTAIN"}),
])
def test_http_clock_is_sampled_after_connection_wait(
    core, tmp_path, monkeypatch, route, function, payload,
):
    capture, _ = captured(core, tmp_path)
    entered, release, observed = threading.Event(), threading.Event(), []
    owner_thread = threading.get_ident()

    class GatedStore(Store):
        def __init__(self, database, state_directory):
            super().__init__(database, state_directory)
            entered.set()
            assert release.wait(3)

    original = getattr(api, function)

    def observe(*args, **kwargs):
        # The three native operations take their scalar `now` at index 3 or 4.
        now = args[3 if function == "lease_deliveries" else 4]
        observed.append((now, threading.get_ident()))
        return original(*args, **kwargs)

    # begin_send's positional order differs from delivery_result.
    if function == "begin_send":
        def observe(store, message_id, token, now, settings, **kwargs):
            observed.append((now, threading.get_ident()))
            return original(store, message_id, token, now, settings, **kwargs)

    monkeypatch.setattr(api_store, "Store", GatedStore)
    monkeypatch.setattr(api, function, observe)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, headers=headers(core),
        ) as client:
            task = asyncio.create_task(client.post(PREFIX + route, json={
                **payload, **({"authority": capture.authority} if function == "lease_deliveries"
                             else {}),
            }))
            try:
                await event_wait(entered)
                core.clock.advance(2)
                release.set()
                response = await asyncio.wait_for(task, 5)
                assert response.status_code == (200 if function == "lease_deliveries" else 409)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
    assert observed == [(core.clock(), observed[0][1])]
    assert observed[0][1] != owner_thread


@pytest.mark.parametrize("kind", ["asyncio", "anyio"])
@pytest.mark.parametrize("outcome", ["commit", "rollback"])
def test_cancel_joins_native_operation_and_closes_in_own_thread(
    core, tmp_path, monkeypatch, kind, outcome,
):
    capture, _ = captured(core, tmp_path)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    connections = []

    class ObservedStore(Store):
        def __init__(self, database, state_directory):
            super().__init__(database, state_directory)
            connections.append((self, threading.get_ident()))

        def close(self):
            assert threading.get_ident() == connections[0][1]
            super().close()
            closed.set()

    monkeypatch.setattr(api_store, "Store", ObservedStore)
    runner = api_store.ApiStore(core.database, core.settings.state_directory)

    def operation(store):
        # Hold a real write transaction across cancellation. Native admission is
        # performed only for the successful branch; failure rolls back its write.
        if outcome == "rollback":
            with store.transaction():
                store.execute("INSERT INTO runtime_meta VALUES ('cancel_probe','temporary')")
                entered.set()
                assert release.wait(3)
                raise RuntimeError("synthetic transaction failure")
        entered.set()
        assert release.wait(3)
        return owner_input.approve(store, core.settings, core.clock, capture)

    async def scenario():
        scope = anyio.CancelScope()

        async def request():
            with scope:
                return await runner.run(operation)

        task = asyncio.create_task(request())
        await event_wait(entered)
        if kind == "asyncio":
            task.cancel()
        else:
            scope.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and not closed.is_set()
        if kind == "asyncio":
            task.cancel()  # Repeated shutdown cancellation must still join.
        release.set()
        if kind == "asyncio":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        else:
            await asyncio.wait_for(task, 5)
        assert closed.is_set()

    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert core.store.one("SELECT value FROM runtime_meta WHERE key='cancel_probe'") is None
    if outcome == "commit":
        # The original HTTP response was cancelled, but retry retrieves the
        # committed native receipt rather than admitting a second input.
        assert owner_input.approve(core.store, core.settings, core.clock, capture)["duplicate"]
    else:
        assert core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                              (owner_input.INPUT + capture.event_id,)) is None


def test_constructor_failure_returns_clean_pool_lease_and_preserves_error(core, monkeypatch):
    errors, pids = [], []

    def fail(connection):
        pids.append(connection.info.backend_pid)
        connection.execute("BEGIN")
        connection.execute("INSERT INTO runtime_meta VALUES ('lease-setup-probe','1')")
        try:
            connection.execute("SELECT * FROM missing_session_setup_table")
        except psycopg.errors.UndefinedTable as error:
            errors.append(error)
            raise

    with postgres.Database(core.settings.database, max_connections=1) as database:
        with monkeypatch.context() as patch:
            patch.setattr(postgres, "Session", fail)
            with pytest.raises(psycopg.errors.UndefinedTable) as caught:
                Store(database, core.settings.state_directory)
        assert len(errors) == 1 and errors[0] is caught.value
        assert caught.value.sqlstate == "42P01"
        with closing(Store(database, core.settings.state_directory)) as recovered:
            assert pids == [recovered.backend_pid]
            assert not recovered.in_transaction
            assert recovered.one("SELECT 1 FROM runtime_meta WHERE key='lease-setup-probe'") is None


def test_owner_check_waits_for_fsynced_uncommitted_authority(
    core, tmp_path, monkeypatch, record_property,
):
    from types import SimpleNamespace

    capture, _ = captured(core, tmp_path)
    policy = owner_authority.current(core.store, core.settings, core.clock).policy
    entered, release = threading.Event(), threading.Event()
    original_append = owner_authority._append

    def paused_append(*args, **kwargs):
        result = original_append(*args, **kwargs)
        # The real append has fsynced its journal and updated the writer's DB
        # projection, but activate's real outer BEGIN has not committed yet.
        entered.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(owner_authority, "_append", paused_append)

    def writer():
        store = Store(core.database, core.settings.state_directory)
        try:
            runtime = SimpleNamespace(store=store, settings=core.settings, clock=core.clock)
            return activate(runtime, policy, revision=1, operation="thread-journal-writer")[0]
        finally:
            store.close()

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, headers=headers(core),
        ) as client, httpx.AsyncClient(base_url=client.base_url, trust_env=False) as probe:
            assert (await probe.get(PREFIX + "/health/live")).status_code == 200
            writing = asyncio.create_task(asyncio.to_thread(writer))
            checking = None
            try:
                await event_wait(entered)
                checking = asyncio.create_task(client.post(PREFIX + "/owner-authority/check",
                    json={"authority": capture.authority, "purpose": "chat"}))
                await asyncio.sleep(0.05)
                before = perf_counter()
                health = await asyncio.wait_for(probe.get(PREFIX + "/health/live"), 0.75)
                elapsed = perf_counter() - before
                record_property("health_during_journal_commit_gap_ms", round(elapsed * 1000, 3))
                assert health.status_code == 200 and elapsed < 0.75
                assert not checking.done()
                release.set()
                committed = await asyncio.wait_for(writing, 5)
                response = await asyncio.wait_for(checking, 5)
                assert response.status_code == 409, response.text
                assert response.json()["error"] == "OWNER_AUTHORITY_REVISION_CHANGED"
                current = await client.post(PREFIX + "/owner-authority/check", json={
                    "authority": committed["authority"], "purpose": "chat"})
                assert current.status_code == 200, current.text
            finally:
                release.set()
                await asyncio.gather(writing, *([checking] if checking else []),
                                     return_exceptions=True)
    asyncio.run(scenario())
    for key in ("model_budget_blocked", "model_budget_restore_required"):
        assert core.store.one('SELECT 1 FROM runtime_meta WHERE key=%s', (key,)) is None


def test_real_http_and_independent_sdk_worker_keep_unknown_and_stop_receipts(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    core.settings.control_token = SecretStr("synthetic-owner-control-token")

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, headers=headers(core),
        ) as client:
            checked = await client.post(PREFIX + "/owner-authority/check", json={
                "authority": capture.authority, "purpose": "chat"})
            assert checked.status_code == 200
            approved = await client.post(PREFIX + "/owner-authority/inputs/approve",
                                         json=capture.model_dump(mode="json"))
            assert approved.status_code == 200 and not approved.json()["duplicate"]
            admitted = await client.post(PREFIX + "/inbox/events",
                json=inbound(core, capture).model_dump(mode="json"),
                headers={"X-Idempotency-Key": "thread-http-input"})
            assert admitted.status_code == 202, admitted.text
            configuration = tmp_path / "thread-sdk-worker.pickle"
            configuration.write_bytes(pickle.dumps(
                (core.settings, core.clock.now, admitted.json()["job_id"])))
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", str(Path(__file__).with_name("agent_sdk_worker.py")),
                str(configuration), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await child_done(process)
            leased = await client.post(PREFIX + "/deliveries/lease", json={
                "worker_id": "thread-http-worker", "authority": capture.authority})
            assert leased.status_code == 200, leased.text
            item, = leased.json()["items"]
            token = {"lease_token": item["lease_token"]}
            path = PREFIX + "/deliveries/" + item["message_id"]
            check = {"authority": item["authority"], "message_id": item["message_id"],
                     "event_id": item["origin_event_id"], "content_sha256": item["content_sha256"],
                     "lease_token": item["lease_token"], "reply_revision": item["reply_revision"]}
            assert (await client.post(path + "/begin-send", json=token)).status_code == 200
            assert (await client.post(PREFIX + "/owner-authority/send-check", json=check)).json()[
                "approved"]
            uncertain = await client.post(path + "/result", json={
                **token, "result": "DELIVERY_UNCERTAIN"})
            assert uncertain.status_code == 200, uncertain.text
            assert uncertain.json()["state"] == "DELIVERY_UNCERTAIN"
            # A later lease cannot replay the uncertain delivery or repeat paid I/O.
            repeated = await client.post(PREFIX + "/deliveries/lease", json={
                "worker_id": "thread-http-retry", "authority": capture.authority})
            assert repeated.json()["items"] == []
            stop = await client.post(PREFIX + "/owner-authority/stop", headers={
                "Authorization": "Bearer " + core.settings.control_token.get_secret_value(),
            }, json={"operation_id": "thread-http-stop", "authority": capture.authority})
            assert stop.status_code == 200 and stop.json()["state"] == "STOPPED"
            for suffix, body in [
                ("/owner-authority/check", {"authority": capture.authority, "purpose": "chat"}),
                ("/owner-authority/inputs/approve", capture.model_dump(mode="json")),
                ("/owner-authority/send-check", check),
            ]:
                stopped = await client.post(PREFIX + suffix, json=body)
                assert stopped.status_code in {403, 409}, stopped.text
            assert core.store.one('SELECT state FROM outbox WHERE message_id=%s',
                                  (item["message_id"],))["state"] == "DELIVERY_UNCERTAIN"
            assert core.store.one("SELECT COUNT(*) n FROM model_usage WHERE state='SETTLED'")[
                "n"] == 1
            assert core.store.one('SELECT attempt_count FROM outbox WHERE message_id=%s',
                                  (item["message_id"],))["attempt_count"] == 1
    asyncio.run(scenario())
