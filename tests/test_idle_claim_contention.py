"""Idle chat polls must leave the SQLite writer available to delivery."""

import sqlite3
from contextlib import closing

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
        with closing(sqlite3.connect(settings.database, timeout=0)) as writer:
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
            assert store.one("SELECT state FROM jobs WHERE job_id=?",
                             (admitted["job_id"],))["state"] == "QUEUED"
            with closing(sqlite3.connect(settings.database, timeout=0)) as writer:
                writer.execute("UPDATE jobs SET state='RUNNING',lease_owner='other',"
                               "lease_token='original-token',lease_until=? WHERE job_id=?",
                               ("2099-01-01T00:00:00+00:00", admitted["job_id"]))
                writer.commit()
        return proactive_allowed(store, settings, now)

    monkeypatch.setattr("companion.core.proactive_allowed", check)
    assert core.claim_job("stale-snapshot") is None
    row = core.store.one("SELECT * FROM jobs WHERE job_id=?", (admitted["job_id"],))
    assert (row["lease_owner"], row["lease_token"]) == ("other", "original-token")
    assert checked == [False, True]
