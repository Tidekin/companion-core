"""A stop request ends worker loops only between units, never inside one."""

import asyncio
import importlib
import json
import os
import secrets
import signal
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from time import monotonic

import pytest

from companion import __main__, shutdown, worker_loop
from companion import core as core_module
from companion import life_worker as life_module
from companion.autonomous_selector import Selection
from companion.store import utcnow
from companion.worker_lock import single_worker
from companion.worker_schedule import UnitResult
from companion.worker_wakeup import Wakeup
from tests.conftest import run
from tests.support.worker_shutdown import NoNetworkClient

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def stop_flag():
    shutdown.reset()
    yield
    shutdown.reset()


@pytest.fixture
def chat_worker(core, monkeypatch):
    monkeypatch.setattr(core_module, "Core", lambda *_: core)
    monkeypatch.setattr(__main__.httpx, "AsyncClient", lambda **_: NoNetworkClient())
    return core


def test_stop_between_units_exits_without_claiming_another(chat_worker, monkeypatch):
    units = []

    async def unit(worker_id):
        units.append(worker_id)
        return True

    original = worker_loop.Recorder.completed

    def completed(self, **kwargs):
        original(self, **kwargs)
        shutdown.request()

    monkeypatch.setattr(chat_worker, "run_once", unit)
    monkeypatch.setattr(worker_loop.Recorder, "completed", completed)
    assert run(asyncio.wait_for(__main__.worker(chat_worker.settings), 5)) is None
    assert len(units) == 1


def test_stop_during_unit_lets_it_complete_then_exits(chat_worker, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    started, finished = [], []

    async def unit(worker_id):
        started.append(worker_id)
        entered.set()
        await release.wait()
        finished.append(worker_id)
        return True

    monkeypatch.setattr(chat_worker, "run_once", unit)

    async def scenario():
        task = asyncio.create_task(__main__.worker(chat_worker.settings))
        await asyncio.wait_for(entered.wait(), 5)
        shutdown.request(getattr(signal, "SIGTERM", None))
        await asyncio.sleep(0.05)
        assert not task.done() and finished == []
        release.set()
        await asyncio.wait_for(task, 5)

    run(scenario())
    assert len(started) == len(finished) == 1


@pytest.mark.parametrize("before_wait", [True, False])
def test_idle_wait_returns_promptly_on_stop(core, before_wait):
    async def scenario():
        with Wakeup(core.store, core.settings, "worker", utcnow) as wake:
            scan = wake.before_scan()
            if before_wait:
                shutdown.request()
            pending = asyncio.create_task(
                wake.wait(scan, UnitResult(False), monotonic() + 30),
            )
            await asyncio.sleep(0.1)
            started = monotonic()
            shutdown.request()
            assert await asyncio.wait_for(pending, 2) == "shutdown"
            assert monotonic() - started < 1
            # A later Wakeup close or cancellation still uses its own stop event.
            assert not wake.stop.is_set()

    run(scenario())


@pytest.mark.parametrize(
    "module_name,entry,service,operation",
    [
        ("companion.content_worker", "content_worker", "companion.content.ContentService",
         "run_batch"),
        ("companion.executive_worker", "executive_worker", "companion.executive.Executive",
         "run_one"),
    ],
)
def test_runtime_workers_stop_from_idle_wait(
    core, monkeypatch, module_name, entry, service, operation,
):
    module_path, class_name = service.rsplit(".", 1)
    service_class = getattr(importlib.import_module(module_path), class_name)
    units = []

    async def unit(_instance, *_args):
        units.append(1)
        # Request the stop once this worker is about to idle.
        asyncio.get_running_loop().call_later(0.1, shutdown.request)
        return False

    monkeypatch.setattr(service_class, operation, unit)
    module = importlib.import_module(module_name)
    assert run(asyncio.wait_for(getattr(module, entry)(core.settings), 5)) is None
    assert units == [1]


def test_life_worker_stops_between_selections(core, monkeypatch):
    steps = []

    class Selector:
        next_maintenance = next_blocked_maintenance = None

        async def step(self):
            steps.append(1)
            if len(steps) == 2:
                shutdown.request()
            return Selection("lane", UnitResult(True), True)

        def resume(self):
            pass

    monkeypatch.setattr(life_module, "Core", lambda *_: core)
    monkeypatch.setattr(life_module.httpx, "AsyncClient", lambda **_: NoNetworkClient())
    monkeypatch.setattr(life_module, "native_selector", lambda *_: Selector())
    assert run(asyncio.wait_for(life_module.life_worker(core.settings), 5)) is None
    assert steps == [1, 1]


def test_install_registers_handlers_and_second_interrupt_escapes(monkeypatch):
    installed = {}
    monkeypatch.setattr(shutdown.signal, "signal",
                        lambda number, handler: installed.__setitem__(number, handler))
    shutdown.install()
    assert installed == {signal.SIGTERM: shutdown.request, signal.SIGINT: shutdown.request}
    shutdown.request(signal.SIGINT)
    assert shutdown.requested()
    with pytest.raises(KeyboardInterrupt):
        shutdown.request(signal.SIGINT)
    shutdown.request(signal.SIGTERM)


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal delivery to a real worker")
def test_real_worker_cli_exits_zero_on_sigterm_and_releases_lock(tmp_path):
    database = tmp_path / "companion.sqlite3"
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("COMPANION_", "ONEBOT_", "SNOWLUMA_"))}
    env.update({
        "COMPANION_CONFIG": str(tmp_path / "absent.toml"), "COMPANION_MODE": "demo",
        "COMPANION_OWNER_QQ_ID": "10001", "COMPANION_BOT_QQ_ID": "10002",
        "COMPANION_BRIDGE_TOKEN": secrets.token_urlsafe(32),
        "COMPANION_DATABASE": str(database),
        "COMPANION_ERASURE_LEDGER": str(tmp_path / "erasures.jsonl"),
        "COMPANION_MEMORY_ENABLED": "false", "COMPANION_MODEL_KEY": "",
        "COMPANION_MODEL_URL": "https://synthetic.invalid/v1",
    })

    async def scenario():
        init = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-m", "companion", "init", cwd=ROOT, env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(init.communicate(), 60)
        assert init.returncode == 0, error.decode(errors="replace")[-2000:]
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-m", "companion", "worker", cwd=ROOT, env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            deadline = monotonic() + 60
            while True:
                assert process.returncode is None and monotonic() < deadline
                with closing(sqlite3.connect(database)) as view:
                    row = view.execute("SELECT value FROM runtime_meta WHERE key=?",
                                       (worker_loop.KEY,)).fetchone()
                if row and json.loads(row[0])["stage"].startswith("WAITING"):
                    break
                await asyncio.sleep(0.1)
            process.send_signal(signal.SIGTERM)
            _, error = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode == 0, error.decode(errors="replace")[-2000:]
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        with single_worker(str(database)):
            pass

    run(scenario())
