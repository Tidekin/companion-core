"""Each synchronous boundary sees new native rows, even in the same transaction."""

from pathlib import Path

import pytest

from companion import owner_authority, runtime_config
from companion.agent_runtime import check_conversation
from companion.contracts import CoreError
from tests.support.context_planner import initial
from tests.support.owner_runtime import chat


@pytest.mark.parametrize(("change", "code"), [
    ("stop", "OWNER_AUTHORITY_DISABLED"),
    ("lease", "JOB_LEASE_LOST"),
    ("source", "OWNER_SOURCE_CHANGED"),
    ("receipt", "OWNER_SOURCE_CHANGED"),
    ("reply", "REPLY_SUPERSEDED"),
    ("configuration", "CONFIGURATION_TASK_BINDING_INVALID"),
    ("privacy", "CONTEXT_PLAN_INVALID"),
    ("journal", "OWNER_AUTHORITY_CUSTODY_REQUIRED"),
])
def test_next_guard_rechecks_changes_inside_the_same_transaction(core, tmp_path, change, code):
    capture, job = chat(core, tmp_path)
    key = "job:" + job["job_id"]
    runtime_config.bind(core.store, core.settings, core.clock, key,
                        snapshot=runtime_config._snapshot(core.settings))
    if change == "privacy":
        # Plans accept a newer epoch; make a later rollback fall below this plan's epoch.
        core.store.execute("INSERT INTO runtime_meta(key,value) VALUES ('privacy_epoch','1') "
                           "ON CONFLICT(key) DO UPDATE SET value="
                           "(runtime_meta.value::bigint+1)::text")
    _, snapshots = initial(core, job, references=0, turns=0)
    with core.store.transaction():
        assert check_conversation(core, job, snapshots).authority.as_dict() == capture.authority
        if change == "stop":
            core.store.execute("UPDATE runtime_meta SET value='OFF' WHERE key=%s",
                               (owner_authority.MODE,))
        elif change == "lease":
            core.store.execute("UPDATE jobs SET lease_token='replaced' WHERE job_id=%s",
                               (job["job_id"],))
        elif change == "source":
            core.store.execute("UPDATE events SET redacted=1 WHERE event_id=%s",
                               (job["event_id"],))
        elif change == "receipt":
            core.store.execute("DELETE FROM runtime_meta WHERE key=%s",
                               ("owner_input:" + job["event_id"],))
        elif change == "reply":
            core.store.execute("UPDATE conversations SET reply_revision=reply_revision+1 "
                               "WHERE conversation_id=%s", (job["conversation_id"],))
        elif change == "configuration":
            core.store.execute("UPDATE operator_task_configuration SET binding_sha256=%s "
                               "WHERE task_key=%s", ("0" * 64, key))
        elif change == "privacy":
            core.store.execute("UPDATE runtime_meta SET value=(value::bigint-1)::text "
                               "WHERE key='privacy_epoch'")
        else:
            path = Path(core.settings.paid_model.journal_path)
            path.write_bytes(path.read_bytes().replace(b'"previous":"0', b'"previous":"9', 1))
        with pytest.raises(CoreError, match=code):
            check_conversation(core, job, snapshots)
    assert not core.store.rows("SELECT * FROM model_usage")
    assert not core.store.rows("SELECT * FROM outbox")
