"""Independent native memory workers claim the original shared checkpoint once."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from companion import budget, memory_work
from companion.core import Core
from companion.store import after, encode
from companion.worker_schedule import UnitResult
from tests.conftest import ingest, run


def test_initial_memory_claim_serializes_independent_workers(core, monkeypatch):
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    ingest(core, 1, "合成项目代号是青栎")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    first_entered, second_entered, release = (threading.Event() for _ in range(3))
    original = memory_work.extraction_sources
    phases = []

    def sources(worker, local_day):
        if worker is core:
            first_entered.set()
            assert release.wait(10)
        else:
            second_entered.set()
        return original(worker, local_day)

    async def phase(worker, local_day, lease, batch, persist):
        phases.append(lease)
        batch["phase"] = "EXTRACT"
        return UnitResult(False)

    monkeypatch.setattr(memory_work, "extraction_sources", sources)
    monkeypatch.setattr(memory_work, "extraction_step", phase)
    with Core(core.settings, core.model, core.clock) as other, ThreadPoolExecutor(2) as pool:
        pid = other.store.one("SELECT pg_backend_pid() AS pid")["pid"]
        first = pool.submit(run, core.extract_step())
        try:
            assert first_entered.wait(10)
            second = pool.submit(run, other.extract_step())
            deadline = time.monotonic() + 10
            with core.database.session() as observer:
                while not second_entered.is_set():
                    state = observer.one(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (pid,))
                    if state and state["wait_event_type"] == "Lock":
                        break
                    assert time.monotonic() < deadline
                    time.sleep(.02)
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)
    assert len(phases) == 1
    assert not core.model.calls
    record = memory_work.checkpoint_state(core.store)
    assert record["next_attempt_at"] > core.clock()
    assert core.store.one(
        "SELECT 1 FROM runtime_meta WHERE key='memory_extraction_lease'") is None


def test_no_due_memory_work_does_not_leave_a_lease(core):
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 1
    assert not run(core.extract_step()).progressed
    assert memory_work.checkpoint_state(core.store) is None
    assert core.store.one(
        "SELECT 1 FROM runtime_meta WHERE key='memory_extraction_lease'") is None
    assert not core.model.calls


def test_expired_memory_lease_does_not_change_another_request(core):
    core.settings.memory_enabled = True
    core.settings.extraction_min_events = 1
    core.settings.extraction_idle_seconds = 0
    ingest(core, 1, "合成项目代号是青栎")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    budget.reserve(core.store, core.settings, None, "memory_extract",
                   [{"role": "user", "content": "另一个保留请求"}], core.clock())
    usage = core.store.rows("SELECT * FROM model_usage")
    assert len(usage) == 1 and usage[0]["state"] == "RESERVED"
    core.store.execute("INSERT INTO runtime_meta(key,value) VALUES "
                       "('memory_extraction_lease',%s)", (
                           encode({"token": "expired", "until": after(core.clock(), -1)}),))
    assert run(core.extract_step()).progressed
    assert core.store.rows("SELECT * FROM model_usage WHERE request_id=%s", (
        usage[0]["request_id"],)) == usage
