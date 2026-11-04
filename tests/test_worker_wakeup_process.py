"""Real CLI processes share one SQLite authority; only external model transport is mocked."""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr

from companion import owner_authority, owner_input
from companion.contracts import Candidate
from companion.owner_authority_protocol import OwnerCapture, OwnerGrantPolicy
from companion.store import after, utcnow
from companion_delivery_protocol.authority import capture_signing_payload
from tests.conftest import ingest, run
from tests.support.core_http import real_core_http
from tests.support.owner_authority import activate
from tests.test_onebot_capture import pinned_plan
from tests.test_owner_input import captured, inbound
from tests.test_private_companion import companion_options


async def start(
    core, directory, role="worker", *, duration=5, settle=0.4, controlled=False,
):
    directory.mkdir(parents=True)
    config, ready, report = (directory / name for name in ("config.json", "ready", "result.json"))
    config.write_text(
        json.dumps(
            {
                "settings": core.settings.model_dump(mode="python"),
                "role": role,
                "duration": duration,
                "settle": settle,
                "ready": str(ready),
                "report": str(report),
                "release": str(directory / "release") if controlled else None,
                "scan_progress": str(directory / "scans") if controlled else None,
            },
            default=lambda v: v.get_secret_value() if isinstance(v, SecretStr) else str(v),
        ),
        encoding="utf-8",
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-c",
        # Load SDK type declarations before the child instruments provider HTTP.
        "import pydantic_ai; from tests.worker_wakeup_process import main; main()",
        str(config),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return process, ready, report


async def ready(child):
    await wait_progress(child, child[1].exists, "ready", 15)


async def wait_progress(child, observed, expected, seconds):
    process, ready_path, report = child
    try:
        async with asyncio.timeout(seconds):
            while process.returncode is None:
                if observed():
                    return
                await asyncio.sleep(0.02)
    except TimeoutError:
        pass
    exit_at_failure = process.returncode
    # Let the bounded child snapshot its last native phase before draining stderr.
    ready_path.with_name("release").touch()
    drain = asyncio.create_task(process.communicate())
    try:
        _, stderr = await asyncio.wait_for(asyncio.shield(drain), 2)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
        _, stderr = await drain
    result = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
    scans = ready_path.with_name("scans")
    raise AssertionError({
        "expected": expected, "exit_at_failure": exit_at_failure, "returncode": process.returncode,
        "ready": ready_path.exists(), "phase": result.get("phase", "no final report"),
        "last_completed_scan": scans.read_text() if scans.exists() else None,
        "counters": result.get("total"), "stderr": stderr.decode(errors="replace")[-4000:],
    })


async def next_scan(child, previous):
    path = child[1].with_name("scans")
    await wait_progress(child, lambda: int(path.read_text()) > previous,
                        f"completed scan after {previous}", 5)


async def finish(child, *, code=0):
    process, _, report = child
    output, error = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode == code, (
        output.decode(errors="replace"),
        error.decode(errors="replace"),
    )
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["pid"] != os.getpid()
    assert result["watchers"] == result["closed_watchers"]
    return result


async def close_child(child):
    process = child[0]
    if process.returncode is None:
        process.kill()
    await process.communicate()


def signed_input(core, template, key, number):
    value = template.model_dump(mode="json", exclude={"signature"})
    source = template.source_event_id.rpartition(":")[0] + ":" + str(number)
    stamp = core.clock()
    value.update(source_event_id=source,
                 event_id="evt_" + hashlib.sha256(source.encode()).hexdigest(),
                 occurred_at=stamp, received_at=stamp)
    value["signature"] = key.sign(capture_signing_payload(value)).hex()
    capture = OwnerCapture.model_validate(value)
    message = inbound(core, capture).model_copy(update={"occurred_at": capture.occurred_at})
    return capture, message


async def state(core, job_id, wanted="SUCCEEDED", seconds=3):
    started = monotonic()
    async with asyncio.timeout(seconds):
        while core.store.one("SELECT state FROM jobs WHERE job_id=?", (job_id,))["state"] != wanted:  # noqa: ASYNC110 - observes an independent process
            await asyncio.sleep(0.01)
    return monotonic() - started


def receipt(name, value):
    directory = os.getenv("WORKER_WAKEUP_RECEIPTS")
    if directory:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        (path / (name + ".json")).write_text(json.dumps(value, indent=2), encoding="utf-8")


def test_four_native_entrypoints_use_shared_scan_and_unit_boundary(core, tmp_path):
    core.settings.content_enabled = True
    core.settings.executive = core.settings.executive.model_copy(update={"enabled": True})
    roles = ("worker", "content-worker", "executive-worker", "life-worker")

    async def scenario():
        children = await asyncio.gather(*[
            start(core, tmp_path / role, role, duration=3, settle=0.4) for role in roles
        ])

        async def checked(child, role, operation):
            try:
                return await operation(child)
            except TimeoutError as error:
                process, ready_path, report_path = child
                error.add_note(json.dumps({"role": role, "pid": process.pid,
                    "returncode": process.returncode, "ready": ready_path.exists(),
                    "report": report_path.exists()}))
                raise

        try:
            async with asyncio.TaskGroup() as group:
                for child, role in zip(children, roles, strict=True):
                    group.create_task(checked(child, role, ready))
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(checked(child, role, finish))
                         for child, role in zip(children, roles, strict=True)]
            rows = [task.result() for task in tasks]
            for row in rows:
                assert row["total"]["scan_contexts"] > 0
                assert row["total"]["native_units"] > 0
                assert row["total"]["provider_calls"] == 0
                assert row["watchers"] == row["closed_watchers"] == 1
            receipt("shared-boundary-four-workers", rows)
        finally:
            await asyncio.gather(*[close_child(child) for child in children])

    run(scenario())


@pytest.mark.parametrize("phase", ["ready", "finish"])
def test_four_native_workers_cancel_waiters_before_failure_cleanup(
    core, tmp_path, monkeypatch, phase,
):
    children, active, entered, closed, drained = [], set(), set(), set(), set()
    ready_calls, finish_calls = [], []
    gate = asyncio.Event()
    original_ready = ready

    async def blocked(role):
        active.add(role)
        entered.add(role)
        if len(entered) == 4:
            gate.set()
        try:
            await gate.wait()
            if role == "worker":
                raise TimeoutError("SYNTHETIC_NATIVE_WORKER_WAIT")
            await asyncio.Future()
        finally:
            active.remove(role)

    class Process:
        def __init__(self, role):
            self.role, self.pid, self.returncode = role, 1000 + len(children), None

        def kill(self):
            assert not active, "All waiting tasks must finish cancellation before closing children"
            self.returncode = -9
            closed.add(self.role)

        async def communicate(self):
            if self.returncode is not None:
                drained.add(self.role)
                return b"", b""
            finish_calls.append(self.role)
            await blocked(self.role)

    async def controlled_start(core, directory, role, **options):
        directory.mkdir()
        child = Process(role), directory / "ready", directory / "result.json"
        children.append(child)
        if phase == "finish":
            child[1].write_text("ready", encoding="utf-8")
        return child

    async def controlled_ready(child):
        ready_calls.append(child[0].role)
        if phase == "ready":
            await blocked(child[0].role)
        else:
            await original_ready(child)

    monkeypatch.setattr(sys.modules[__name__], "start", controlled_start)
    monkeypatch.setattr(sys.modules[__name__], "ready", controlled_ready)
    with pytest.raises((TimeoutError, ExceptionGroup)) as caught:
        test_four_native_entrypoints_use_shared_scan_and_unit_boundary(core, tmp_path)
    roles = {"worker", "content-worker", "executive-worker", "life-worker"}
    assert closed == drained == set(ready_calls) == roles
    assert not active
    assert set(finish_calls) == (roles if phase == "finish" else set())
    error = caught.value.exceptions[0] if isinstance(caught.value, ExceptionGroup) else caught.value
    assert isinstance(error, TimeoutError)
    assert json.loads(error.__notes__[0]) == {
        "role": "worker", "pid": 1000, "returncode": None,
        "ready": phase == "finish", "report": False,
    }


def test_real_http_enqueue_new_owner_and_independent_worker_latency(core, tmp_path):
    core.clock = utcnow
    template, key = captured(core, tmp_path)

    async def scenario():
        async with real_core_http(core) as port:
            directory = tmp_path / "chat"
            # Measured latency starts at enqueue; the child must outlive slow parent setup.
            child = await start(core, directory, duration=30, controlled=True)
            try:
                await ready(child)
                latencies = []
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}",
                    trust_env=False,
                    headers={
                        "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()
                    },
                ) as api:
                    for number in (1, 2):
                        capture, message = signed_input(core, template, key, number)
                        approved = await api.post("/internal/v1/owner-authority/inputs/approve",
                                                  json=capture.model_dump(mode="json"))
                        assert approved.status_code == 200, approved.text
                        response = await api.post(
                            "/internal/v1/inbox/events",
                            json=message.model_dump(mode="json"),
                            headers={"X-Idempotency-Key": f"wake-{number}"},
                        )
                        assert response.status_code == 202, response.text
                        latency = await state(core, response.json()["job_id"])
                        assert latency < 1.5
                        latencies.append(latency)
                (directory / "release").touch()
                result = await finish(child)
                assert result["total"]["provider_calls"] == 2
                receipt("enqueue-latency", {"latencies_seconds": latencies, "process": result})
            finally:
                await close_child(child)

    run(scenario())


def test_independent_retry_due_without_new_write(core, tmp_path):
    core.clock = utcnow
    template, key = captured(core, tmp_path)
    capture, message = signed_input(core, template, key, 1)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    item = core.ingest(message, "wake-retry")
    # Keep the job pending while the independent interpreter imports/starts.
    due = after(utcnow(), 3600)
    core.store.execute(
        "UPDATE jobs SET state='RETRY_WAIT',next_attempt_at=? WHERE job_id=?", (due, item["job_id"])
    )

    async def scenario():
        child = await start(core, tmp_path / "retry", duration=5)
        try:
            await ready(child)
            assert core.store.one("SELECT state FROM jobs WHERE job_id=?", (item["job_id"],))[
                "state"
            ] == ("RETRY_WAIT")
            core.store.execute("UPDATE jobs SET next_attempt_at=? WHERE job_id=?",
                               (after(utcnow(), 1), item["job_id"]))
            # No writes after scheduling: only the native deadline can make it due.
            await state(core, item["job_id"], seconds=5)
            result = await finish(child)
            assert result["total"]["provider_calls"] == 1
            receipt("retry-without-writer", result)
        finally:
            await close_child(child)

    run(scenario())


def test_controlled_worker_reports_idle_phase_on_progress_timeout(core, tmp_path):
    async def scenario():
        child = await start(core, tmp_path / "diagnostic", duration=30, settle=60, controlled=True)
        try:
            await ready(child)
            with pytest.raises(AssertionError) as caught:
                await wait_progress(child, lambda: False, "unreachable scan", 0.05)
            detail = caught.value.args[0]
            assert detail["expected"] == "unreachable scan" and detail["ready"]
            assert detail["exit_at_failure"] is None and detail["returncode"] == 0
            assert detail["phase"] == "waiting" and detail["counters"]["wait_entries"] >= 1
            assert int(detail["last_completed_scan"]) >= 1 and "stderr" in detail
        finally:
            await close_child(child)

    run(scenario())


def test_real_http_forget_wakes_native_control_without_provider(core, tmp_path):
    core.clock = utcnow
    core.settings.memory_enabled = True
    source = ingest(core, 1, "I prefer green tea.")
    with core.store.transaction():
        memory_id = core.memory.add(
            Candidate(
                subject="user",
                kind="preference",
                fact_key="tea",
                content="I prefer green tea.",
                evidence_event_id=source["event_id"],
                evidence_quote="I prefer green tea.",
            ),
            core.clock(),
            confirmed=True,
        )
        core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    captured(core, tmp_path)
    view = owner_authority.current(core.store, core.settings, core.clock)
    policy = OwnerGrantPolicy.model_validate({**view.policy.model_dump(),
        "purposes": ["chat", "memory"], "memory_scope": "owner_private"})
    activate(core, policy, revision=view.authority.revision, operation="wakeup-memory-policy")

    async def scenario():
        async with real_core_http(core) as port:
            directory = tmp_path / "forget"
            # The parent can take seconds to send the decision on a loaded runner; wait for the
            # worker's own scan progress instead of racing a fixed child lifetime.
            child = await start(core, directory, duration=30, controlled=True)
            try:
                await ready(child)
                scans = int((directory / "scans").read_text())
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}", trust_env=False
                ) as api:
                    response = await api.post(
                        "/internal/v1/memories/" + memory_id + "/decisions",
                        json={"actor_id": "owner", "decision": "forget"},
                        headers={
                            "Authorization": "Bearer "
                            + core.settings.bridge_token.get_secret_value()
                        },
                    )
                    assert response.status_code == 200, response.text
                await next_scan(child, scans)
                (directory / "release").touch()
                result = await finish(child)
                assert result["total"]["provider_calls"] == 0
                assert result["steady"]["scans"] >= 1
                assert core.store.one(
                    "SELECT status FROM memories WHERE memory_id=?", (memory_id,)
                ) == {"status": "FORGOTTEN"}
            finally:
                await close_child(child)

    run(scenario())


def test_stop_api_wakes_independent_worker_and_lock_remains_exclusive(core, tmp_path):
    core.clock.now = datetime.fromisoformat(utcnow())

    async def scenario():
        options = companion_options(core)
        options["window_options"].update(
            starts_at=datetime.fromisoformat(after(utcnow(), -60))
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .isoformat(),
            ends_at=datetime.fromisoformat(after(utcnow(), 3600))
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .isoformat(),
        )
        async with real_core_http(core) as port:
            cfg, plan = pinned_plan(
                core,
                tmp_path,
                3001,
                core_port=port,
                live=True,
                approved=True,
                scope_options=options,
            )
            core.clock.now = datetime.fromisoformat(utcnow())
            async with httpx.AsyncClient(
                base_url=cfg.core_url,
                trust_env=False,
                headers={"Authorization": "Bearer " + cfg.bridge_token},
            ) as api:
                opened = await api.post(
                    "/internal/v1/live-text/open", json={"window_sha256": plan.sha256}
                )
                assert opened.status_code == 200, opened.text
                directory = tmp_path / "stop"
                child = await start(core, directory, duration=30, controlled=True)
                try:
                    await ready(child)
                    duplicate = await start(core, tmp_path / "duplicate", duration=1)
                    denied = await finish(duplicate, code=2)
                    assert denied["watchers"] == 0
                    scans = int((directory / "scans").read_text())
                    stopped = await api.post(
                        "/internal/v1/live-text/stop", json={"window_sha256": plan.sha256}
                    )
                    assert stopped.status_code == 200, stopped.text
                    await next_scan(child, scans)
                finally:
                    (directory / "release").touch()
                    await child[0].communicate()
                result = await finish(child)
                assert result["steady"]["scans"] >= 1
                assert result["total"]["provider_calls"] == 0
                restarted = await start(core, tmp_path / "restart", duration=1, settle=0.1)
                assert (await finish(restarted))["watchers"] == 1

    run(scenario())
