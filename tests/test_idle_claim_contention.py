"""Idle chat polls leave delivery writers free to commit."""

import asyncio
from contextlib import closing

import psycopg
import pytest

from companion.core import proactive_allowed
from tests.conftest import event


def test_idle_authority_check_allows_independent_writer(core, monkeypatch):
    core.settings.proactive_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    checked = []
    recover = core.recover

    def recover_once(now):
        checked.append("recover")
        assert core.store.in_write_transaction
        return recover(now)

    def check(store, settings, now):
        with closing(psycopg.connect(settings.database.get_secret_value())) as writer:
            writer.execute("INSERT INTO runtime_meta VALUES ('delivery_writer_probe','1')")
            writer.commit()
        checked.append("check")
        return proactive_allowed(store, settings, now)

    monkeypatch.setattr(core, "recover", recover_once)
    monkeypatch.setattr("companion.core.proactive_allowed", check)
    assert core.claim_job("idle") is None
    assert checked == ["recover", "check"]
    assert core.store.one("SELECT value FROM runtime_meta WHERE key='delivery_writer_probe'")


def test_claim_rechecks_job_after_snapshot_changes(core, monkeypatch):
    core.settings.proactive_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    admitted = core.ingest(event(core), "claim-race")
    checked = []

    def check(store, settings, now):
        checked.append(store.in_write_transaction)
        if len(checked) == 1:
            # Pin the old row, then let another worker take the original lease.
            assert store.one("SELECT state FROM jobs WHERE job_id=%s",
                             (admitted["job_id"],))["state"] == "QUEUED"
            with closing(psycopg.connect(settings.database.get_secret_value())) as writer:
                writer.execute("UPDATE jobs SET state='RUNNING',lease_owner='other',"
                               "lease_token='original-token',lease_until=%s WHERE job_id=%s",
                               ("2099-01-01T00:00:00+00:00", admitted["job_id"]))
                writer.commit()
        return proactive_allowed(store, settings, now)

    monkeypatch.setattr("companion.core.proactive_allowed", check)
    assert core.claim_job("stale-snapshot") is None
    row = core.store.one('SELECT * FROM jobs WHERE job_id=%s', (admitted["job_id"],))
    assert (row["lease_owner"], row["lease_token"]) == ("other", "original-token")
    assert checked == [False, True]


@pytest.mark.parametrize("boundary", ["recovery", "claim", "poke"])
@pytest.mark.parametrize("stop_while_busy", [False, True])
def test_chat_worker_survives_busy_before_claim_without_repeating_a_job(
    core, monkeypatch, boundary, stop_while_busy,
):
    from companion import __main__, shutdown
    from companion import core as core_module
    from tests.conftest import run
    from tests.support.worker_shutdown import NoNetworkClient

    admitted = core.ingest(event(core), "busy-claim") if boundary != "poke" else None
    original = (core.recover if boundary == "recovery" else core._claim_job
                if boundary == "claim" else core_module.poke.claim_ready)
    calls, attempts = [], []
    shutdown.reset()

    with closing(psycopg.connect(core.settings.database.get_secret_value())) as writer:
        def contend(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                # The caller already owns the recovery writer; claim contention
                # is introduced at its own snapshot-to-writer boundary instead.
                if boundary == "recovery":
                    return original(*args, **kwargs)
                writer.execute(
                    "SELECT 1 FROM runtime_meta WHERE key='privacy_epoch' FOR UPDATE")
                asyncio.get_running_loop().call_soon(writer.rollback)
                if stop_while_busy:
                    asyncio.get_running_loop().call_soon(shutdown.request)
            return original(*args, **kwargs)

        if boundary == "recovery":
            claim = core.claim_job

            def claim_once(*args, **kwargs):
                attempts.append(1)
                if len(attempts) == 1:
                    writer.execute(
                        "SELECT 1 FROM runtime_meta WHERE key='privacy_epoch' FOR UPDATE")
                    try:
                        return claim(*args, **kwargs)
                    finally:
                        writer.rollback()
                        if stop_while_busy:
                            shutdown.request()
                return claim(*args, **kwargs)

            monkeypatch.setattr(core, "claim_job", claim_once)
        elif boundary == "claim":
            monkeypatch.setattr(core, "_claim_job", contend)
        else:
            core.settings.mode = "live"
            monkeypatch.setattr(core_module.poke, "enabled", lambda _: True)
            monkeypatch.setattr(core_module.poke, "claim_ready", contend)

        monkeypatch.setattr(core_module, "Core", lambda *_: core)
        monkeypatch.setattr(__main__.httpx, "AsyncClient", lambda **_: NoNetworkClient())
        completed = __main__.Recorder.completed

        def stop_after_unit(recorder, **kwargs):
            completed(recorder, **kwargs)
            shutdown.request()

        monkeypatch.setattr(__main__.Recorder, "completed", stop_after_unit)
        try:
            run(asyncio.wait_for(__main__.worker(core.settings), 12))
        finally:
            shutdown.reset()
    if admitted:
        with closing(psycopg.connect(core.settings.database.get_secret_value())) as observed:
            job = observed.execute("SELECT state,attempts FROM jobs WHERE job_id=%s",
                                   (admitted["job_id"],)).fetchone()
        assert job == (("QUEUED", 0) if stop_while_busy else ("SUCCEEDED", 1))
        assert len(core.model.calls) == int(not stop_while_busy)
    else:
        assert len(calls) == (1 if stop_while_busy else 2)
        assert not core.model.calls
