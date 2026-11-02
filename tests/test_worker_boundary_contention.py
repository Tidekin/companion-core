"""A busy configuration boundary must not end or replay a native worker unit."""

import asyncio
import importlib
import sqlite3
from contextlib import closing

import pytest

from companion import runtime_config, shutdown
from companion.autonomous_selector import Selection
from companion.worker_schedule import UnitResult
from tests.conftest import run
from tests.test_worker_shutdown import NoNetworkClient


@pytest.fixture(autouse=True)
def stop_flag():
    shutdown.reset()
    yield
    shutdown.reset()


@pytest.mark.parametrize("role,service,method", [
    ("worker", "companion.core.Core", "run_once"),
    ("content-worker", "companion.content.ContentService", "run_batch"),
    ("executive-worker", "companion.executive.Executive", "run_one"),
    ("life-worker", "companion.autonomous_selector.RoundRobin", "step"),
])
@pytest.mark.parametrize("stop_while_busy", [False, True])
def test_native_worker_survives_real_boundary_lock(
    core, monkeypatch, role, service, method, stop_while_busy,
):
    boundary = runtime_config.boundary
    attempts, units = [], []
    module = importlib.import_module(
        "companion.__main__" if role == "worker" else "companion." + role.replace("-", "_"))
    entry = "worker" if role == "worker" else role.replace("-", "_")

    with closing(sqlite3.connect(core.settings.database, isolation_level=None)) as writer:
        def contend(store, settings, clock, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                store.db.execute("PRAGMA busy_timeout=0")
                writer.execute("BEGIN IMMEDIATE")
                try:
                    return boundary(store, settings, clock, **kwargs)
                finally:
                    writer.rollback()
                    if stop_while_busy:
                        shutdown.request()
            return boundary(store, settings, clock, **kwargs)

        async def unit(instance, *_):
            units.append(1)
            # The complete configuration is published before any business work.
            assert core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                                  (runtime_config.PREFIX + role,))
            shutdown.request()
            return Selection("lane", UnitResult(True), True) if role == "life-worker" else True

        service_module, class_name = service.rsplit(".", 1)
        monkeypatch.setattr(getattr(importlib.import_module(service_module), class_name),
                            method, unit)
        monkeypatch.setattr(runtime_config, "boundary", contend)
        monkeypatch.setattr("httpx.AsyncClient", lambda **_: NoNetworkClient())
        run(asyncio.wait_for(getattr(module, entry)(core.settings), 5))

    assert attempts == ([1] if stop_while_busy else [1, 1])
    assert units == ([] if stop_while_busy else [1])
    assert not core.store.rows("SELECT * FROM model_usage")


@pytest.mark.parametrize("failure", [sqlite3.OperationalError("no such table: broken"),
                                     RuntimeError("broken configuration"),
                                     asyncio.CancelledError()])
def test_worker_boundary_propagates_other_failures(core, monkeypatch, failure):
    calls = []

    def fail(*_):
        calls.append(1)
        raise failure

    monkeypatch.setattr(runtime_config, "boundary", fail)
    with pytest.raises(type(failure)):
        run(runtime_config.worker_boundary(core.store, core.settings, core.clock))
    assert calls == [1]
