"""Independent workers can first publish the same immutable configuration safely."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

from companion import runtime_config
from companion.store import Store


def test_two_native_worker_boundaries_share_one_first_snapshot(core, monkeypatch):
    barrier = Barrier(2)
    original = Store.one

    def observe(store, statement, parameters=()):
        row = original(store, statement, parameters)
        if (store.in_write_transaction and row is None and isinstance(statement, str)
                and statement.startswith("SELECT 1 FROM operator_runtime_snapshots")):
            barrier.wait(timeout=5)
        return row

    monkeypatch.setattr(Store, "one", observe)

    def publish(worker):
        with closing(Store(core.database, core.settings.state_directory)) as store:
            snapshot = runtime_config.boundary(store, core.settings, core.clock, worker=worker)
            return store.backend_pid, runtime_config.identity(core.settings, snapshot)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, ("first-a", "first-b")))
    assert results[0][0] != results[1][0] != core.store.backend_pid
    assert results[0][1] == results[1][1]
    assert len(core.store.rows("SELECT * FROM operator_runtime_snapshots")) == 1
    assert len(runtime_config.workers(core.store, core.settings)) == 2


def test_erasure_startup_discovers_handlers_before_exclusive_control_scope(core, monkeypatch):
    from companion import erasure

    original, observed = erasure.contract_signature, []

    def discover():
        observed.append(core.store.in_transaction)
        if len(observed) == 1:
            assert not core.store.in_transaction
            with closing(Store(core.database, core.settings.state_directory)) as writer:
                with writer.transaction(control_write=True):
                    writer.execute("INSERT INTO runtime_meta VALUES ('startup_writer_probe','1')")
        return original()

    monkeypatch.setattr(erasure, "contract_signature", discover)
    core.ledger.initialize(core.store)
    assert observed[0] is False
    assert core.store.one("SELECT value FROM runtime_meta WHERE key='startup_writer_probe'")
