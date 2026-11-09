"""Each delivery boundary validates fresh rows without changing prepared obligations."""

import pytest

from companion import owner_authority, owner_delivery, owner_input, runtime_config
from companion.contracts import CoreError
from tests.support.evaluation import work
from tests.support.owner_input import captured, inbound


def prepared(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    result = core.ingest(inbound(core, capture), "delivery-read-source")
    runtime_config.bind(core.store, core.settings, core.clock, "job:" + result["job_id"],
                        snapshot=runtime_config._snapshot(core.settings))
    assert work(core)
    return owner_delivery._job(core.store, result["job_id"])


@pytest.mark.parametrize(("change", "code"), [
    ("stop", "OWNER_AUTHORITY_DISABLED"),
    ("source", "REPLY_SOURCE_RECALLED"),
    ("receipt", "OWNER_SOURCE_CHANGED"),
    ("reply", "REPLY_SUPERSEDED"),
    ("configuration", "CONFIGURATION_TASK_BINDING_INVALID"),
])
def test_next_delivery_boundary_reads_changes_in_the_same_transaction(
    core, tmp_path, change, code,
):
    job = prepared(core, tmp_path)
    usage = core.store.rows("SELECT * FROM model_usage ORDER BY record_sequence")
    outbox = core.store.rows("SELECT * FROM outbox ORDER BY message_id")
    with core.store.transaction():
        owner_delivery._sources(core.store, core.settings, core.clock, job)
        if change == "stop":
            core.store.execute("UPDATE runtime_meta SET value='OFF' WHERE key=%s",
                               (owner_authority.MODE,))
        elif change == "source":
            core.store.execute("UPDATE events SET redacted=1 WHERE event_id=%s",
                               (job["event_id"],))
        elif change == "receipt":
            core.store.execute("DELETE FROM runtime_meta WHERE key=%s",
                               ("owner_input:" + job["event_id"],))
        elif change == "reply":
            core.store.execute("UPDATE conversations SET reply_revision=reply_revision+1 "
                               "WHERE conversation_id=%s", (job["conversation_id"],))
        else:
            core.store.execute("UPDATE operator_task_configuration SET binding_sha256=%s "
                               "WHERE task_key=%s", ("0" * 64, "job:" + job["job_id"]))
        with pytest.raises(CoreError, match=code):
            owner_delivery._sources(core.store, core.settings, core.clock, job)
    assert core.store.rows("SELECT * FROM model_usage ORDER BY record_sequence") == usage
    assert core.store.rows("SELECT * FROM outbox ORDER BY message_id") == outbox


def test_prepared_delivery_keeps_original_unbound_configuration_semantics(core, tmp_path):
    job = prepared(core, tmp_path)
    core.settings.operator_hot_reload = True
    with core.store.transaction():
        core.store.execute("DELETE FROM operator_task_configuration WHERE task_key=%s",
                           ("job:" + job["job_id"],))
        assert owner_delivery._sources(core.store, core.settings, core.clock, job)
