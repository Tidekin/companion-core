"""Native Console effects use independent connections and recheck queued authorization."""

import asyncio
import sys
import threading
from contextlib import closing
from pathlib import Path
from time import perf_counter

import httpx
import pytest
from pydantic import SecretStr

from companion import api_store, console_api
from companion.store import Store, after
from companion.transport import enqueue
from tests.conftest import ingest, run
from tests.support.console_reset_isolation import configure, login
from tests.support.core_http import real_core_http
from tests.support.delegation import delegate, submit
from tests.support.executive import configured, proposal
from tests.support.owner_console import stored_memory
from tests.test_cognition_goal_steps import retained_plan
from tests.test_consolidation import enable_nightly
from tests.test_diary import DAY, generate, seed_day
from tests.test_goals import setup_goal
from tests.test_life_topics import bug

CASES = ["memory-confirm", "memory-correct", "memory-forget", "diary-correct", "diary-forget",
         "mode-diary", "mode-life", "mode-proactive", "goal-accept", "goal-cancel",
         "goal-continue", "reflection", "topic-close", "tool-approve", "tool-revoke",
         "delegation", "adjustment", "reconcile"]


def operation(core, case):
    method = "POST"
    if case.startswith("memory-"):
        core.settings.memory_enabled = True
        memory, _ = stored_memory(core)
        path, body = f"memories/{memory}/decision", {"decision": case.split("-")[1]}
        if case == "memory-correct":
            body["corrected_content"] = "主人喜欢绿茶"
    elif case.startswith("diary-"):
        seed_day(core)
        assert generate(core)["state"] == "COMMITTED"
        action = "correction" if case == "diary-correct" else "forget"
        path = f"diary/{DAY}/{action}"
        body = {"text": "今天继续阅读。"} if action == "correction" else None
    elif case.startswith("mode-"):
        path, body = "mode", {"target": case.split("-")[1], "enabled": False}
    elif case in {"goal-accept", "goal-cancel"}:
        goal = setup_goal(core)
        path, body = f"goals/{goal}/decision", {"decision": case.split("-")[1].upper()}
    elif case == "goal-continue":
        goal, _ = retained_plan(core)
        path, body = f"goals/{goal}/continuation", {
            "expected_version": 1, "action": "WAIT", "wait_until": after(core.clock(), 60)}
    elif case == "reflection":
        enable_nightly(core)
        assert run(core.reflections.run_day(DAY))["state"] == "COMMITTED"
        row = core.store.one("SELECT suggestion_id FROM reflection_suggestions")
        path, body = f"reflections/{row['suggestion_id']}/decision", {"decision": "REJECT"}
    elif case == "topic-close":
        row = bug(core)
        path, body = f"life/topics/{row['topic_id']}/close", {"source_event_id": "evt_test_1"}
    elif case.startswith("tool-"):
        executive = configured(core, allow_changes=True)
        row = executive.submit(proposal(capability="windows.write_text", text="synthetic"))
        path = f"tools/{row['task_id']}/" + ("decision" if case == "tool-approve" else "revoke")
        body = ({"decision": "APPROVE", "scope_sha256": row["scope_sha256"]}
                if case == "tool-approve" else None)
    elif case == "delegation":
        path, method = "autonomy/wake", "PUT"
        body = {"mode": "propose", "allow_pause": True, "ranges": {}}
    elif case == "adjustment":
        core.settings.wake_min_interval_minutes = 180
        delegate(core, mode="propose", ranges={"wake_min_interval_minutes": {"max": 360}})
        row = submit(core, "wake_min_interval_minutes", 300)
        path, body = f"autonomy/adjustments/{row['override_id']}/decision", {"decision": "approve"}
    else:
        event = ingest(core, 1, "合成投递")
        conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                      (event["event_id"],))["conversation_id"]
        message = enqueue(core.store, event["event_id"], conversation,
                          "合成回复", core.clock(), key="synthetic-reconcile")
        core.store.execute("UPDATE outbox SET state='DELIVERY_UNCERTAIN' WHERE message_id=?",
                           (message,))
        path, body = f"deliveries/{message}/reconcile", {"outcome": "SENT"}
    configure(core)
    return method, "/v1/console/" + path, body


@pytest.mark.parametrize("case", CASES)
def test_native_write_wait_keeps_http_responsive_and_retry_idempotent(
    core, monkeypatch, record_property, case,
):
    method, path, body = operation(core, case)
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
            headers = await login(client)
            monkeypatch.setattr(api_store, "Store", ObservedStore)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", str(Path(__file__).with_name("api_store_lock_worker.py")),
                core.settings.database, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            task = None
            try:
                assert (await asyncio.wait_for(process.stdout.readline(), 5)).strip() == b"LOCKED"
                assert (await probe.get("/internal/v1/health/live")).status_code == 200
                task = asyncio.create_task(client.request(method, path, json=body, headers=headers))
                assert await asyncio.to_thread(entered.wait, 3)
                started = perf_counter()
                health = await asyncio.wait_for(probe.get("/internal/v1/health/live"), 0.75)
                elapsed = perf_counter() - started
                record_property("health_during_native_write_wait_ms", round(elapsed * 1000, 3))
                assert health.status_code == 200 and elapsed < 0.75 and not task.done()
                core.clock.advance(2)
                process.stdin.write(b"release\n")
                await process.stdin.drain()
                response = await asyncio.wait_for(task, 8)
                assert response.status_code == 200, response.text
                audit = core.store.rows("SELECT * FROM audit_log")
                repeated = await client.request(method, path, json=body, headers=headers)
                assert repeated.status_code == 200 and repeated.json() == response.json()
                assert core.store.rows("SELECT * FROM audit_log") == audit
                assert core.store.one("SELECT state FROM console_operations")["state"] == "COMPLETE"
                if case == "tool-approve":
                    assert core.store.one("SELECT created_at FROM tool_approvals") == {
                        "created_at": core.clock()}
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


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("when", ["queue", "effect"])
def test_revoked_session_cannot_apply_queued_native_write(core, monkeypatch, case, when):
    method, path, body = operation(core, case)
    entered, release = threading.Event(), threading.Event()
    tables = ("memories", "memory_evidence", "diary_entries", "diary_revisions", "goals",
              "goal_steps", "reflection_suggestions", "life_topics", "tool_tasks", "tool_approvals",
              "outbox", "runtime_meta", "privacy_erasure_ledger", "capability_overrides")
    before = {table: core.store.rows("SELECT * FROM " + table) for table in tables}
    original_operation = console_api._operation

    class GatedStore(Store):
        def __init__(self, path):
            super().__init__(path)
            if when == "queue":
                entered.set()
                assert release.wait(5)

    def gated_operation(core, request, key, body, apply, **kwargs):
        def gated_apply():
            entered.set()
            assert release.wait(5)
            return apply()
        return original_operation(core, request, key, body, gated_apply, **kwargs)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as client:
            headers = await login(client)
            monkeypatch.setattr(api_store, "Store", GatedStore)
            if when == "effect":
                monkeypatch.setattr(console_api, "_operation", gated_operation)
            task = asyncio.create_task(client.request(method, path, json=body, headers=headers))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                with closing(Store(core.settings.database)) as other:
                    other.execute("DELETE FROM console_sessions")
                release.set()
                response = await asyncio.wait_for(task, 8)
                assert response.status_code == 401, response.text
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert {table: core.store.rows("SELECT * FROM " + table) for table in tables} == before
    rows = core.store.rows("SELECT state FROM console_operations")
    assert rows == ([] if when == "queue" else [{"state": "STARTED"}])


@pytest.mark.parametrize("change", ["expiry", "credential", "disabled"])
def test_mode_rechecks_all_session_fences_after_wait(core, monkeypatch, change):
    method, path, body = operation(core, "mode-life")
    original = console_api._operation

    def revoke(core, request, key, material, apply, **kwargs):
        def revoked_apply():
            if change == "expiry":
                core.clock.advance(core.settings.console.session_seconds + 1)
            elif change == "credential":
                core.settings.control_token = SecretStr("rotated-test-credential" * 2)
            else:
                core.settings.console.enabled = False
            return apply()
        return original(core, request, key, material, revoked_apply, **kwargs)

    async def scenario():
        async with real_core_http(core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
        ) as client:
            headers = await login(client)
            monkeypatch.setattr(console_api, "_operation", revoke)
            response = await client.request(method, path, json=body, headers=headers)
            assert response.status_code == (404 if change == "disabled" else 401), response.text

    asyncio.run(scenario())
    assert not core.store.one("SELECT value FROM runtime_meta WHERE key='life_paused'")
