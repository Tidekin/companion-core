"""Native guard batches retain fresh dispatch checks and the original recall key."""

import json

import pytest

from companion import budget, input_recall, owner_authority
from companion.contracts import CoreError
from tests.support.owner_runtime import chat


@pytest.mark.parametrize("stage", ["admit", "dispatch"])
@pytest.mark.parametrize(("change", "code"), [
    ("stop", "OWNER_AUTHORITY_DISABLED"),
    ("lease", "JOB_LEASE_LOST"),
    ("source", "REPLY_SOURCE_RECALLED"),
    ("receipt", "OWNER_SOURCE_CHANGED"),
    ("reply", "REPLY_SUPERSEDED"),
])
def test_next_model_boundary_reads_changes_in_the_same_write_transaction(
    core, tmp_path, stage, change, code,
):
    _, job = chat(core, tmp_path)
    if stage == "dispatch":
        request = budget.reserve(core.store, core.settings, job["job_id"], "chat",
                                 [{"role": "user", "content": "synthetic"}],
                                 core.clock(), clock=core.clock)

    def check():
        if stage == "dispatch":
            owner_authority.check_model(core.store, core.settings, core.clock, request)
        else:
            owner_authority.admit_model(core.store, core.settings, core.clock,
                                        job["job_id"], "chat")

    with core.store.transaction():
        check()
        if change == "stop":
            core.store.execute("UPDATE runtime_meta SET value='OFF' WHERE key=%s",
                               (owner_authority.MODE,))
        elif change == "lease":
            core.store.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00' "
                               "WHERE job_id=%s", (job["job_id"],))
        elif change == "source":
            core.store.execute("UPDATE events SET redacted=1 WHERE event_id=%s",
                               (job["event_id"],))
        elif change == "receipt":
            core.store.execute("DELETE FROM runtime_meta WHERE key=%s",
                               ("owner_input:" + job["event_id"],))
        else:
            core.store.execute("UPDATE conversations SET reply_revision=reply_revision+1 "
                               "WHERE conversation_id=%s", (job["conversation_id"],))
        with pytest.raises(CoreError, match=code):
            check()
    assert not core.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("source", ["ascii-source", "来源:表情🙂", "evt_deleted-source"])
@pytest.mark.parametrize("state", ["RECALLED", "UNMATCHED"])
def test_batched_recall_uses_the_original_utf8_hash_and_evt_presence_contract(core, source, state):
    key = ("erased_event:" + source if source.startswith("evt_")
           else input_recall.marker_key(source))
    assert not input_recall.is_recalled(core.store, source)
    core.store.execute("INSERT INTO runtime_meta(key,value) VALUES (%s,%s)",
                       (key, json.dumps({"state": state})))
    raw = core.store.one(
        "SELECT recalled.value FROM (VALUES (%s::text)) AS e(source_event_id) "
        + input_recall.SOURCE_JOIN_SQL, (source,),
    )
    loaded = {"value": raw["value"]} if raw["value"] is not None else None
    expected = source.startswith("evt_") or state == "RECALLED"
    assert input_recall.is_recalled(core.store, source) is expected
    if expected:
        with pytest.raises(CoreError, match="EVENT_ERASED"):
            input_recall.require_not_recalled(core.store, source, _loaded=loaded)
    else:
        input_recall.require_not_recalled(core.store, source, _loaded=loaded)
