"""A chat reply that finally fails under OwnerAuthority tells the owner, with Core wording."""

import json

import pytest

from companion import owner_delivery
from companion.contracts import CoreError
from companion.transport import enqueue
from tests.owner_model_fixtures import authorized_job
from tests.test_carried_capabilities import reader_carry


def failed(core, tmp_path, code):
    reader_carry(core, tmp_path)
    job_id = authorized_job(core, number=3)
    job = core.store.one("SELECT j.*,e.trace_id,e.conversation_id FROM jobs j "
                         "JOIN events e USING(event_id) WHERE job_id=?", (job_id,))
    core.fail_job(job, code)
    assert core.store.one("SELECT state FROM jobs WHERE job_id=?", (job_id,))["state"] == "FAILED"
    return job


@pytest.mark.parametrize("code,text", [
    ("INTERNAL_ERROR", owner_delivery.FAILURE_TEXT),
    ("SOURCE_GRANT_REQUIRED", owner_delivery.FAILURE_TEXTS["SOURCE_GRANT_REQUIRED"]),
])
def test_final_failure_queues_an_authority_bound_core_notice(core, tmp_path, code, text):
    job = failed(core, tmp_path, code)
    row = core.store.one("SELECT * FROM outbox WHERE origin_event_id=?", (job["event_id"],))
    assert json.loads(row["content_json"]) == {"text": text}
    assert row["state"] == "PENDING" and row["delivery_key"] == f"failure:{job['job_id']}:v1"
    assert json.loads(row["authority_json"]) == json.loads(job["authority_json"])
    assert owner_delivery.check_delivery(
        core.store, core.settings, core.clock, row["message_id"])["approved"] is True
    # Nothing but the fixed wording can ride on this binding.
    core.store.execute("UPDATE outbox SET content_json=? WHERE message_id=?",
                       (json.dumps({"text": "别的内容"}), row["message_id"]))
    with pytest.raises(CoreError, match="OWNER_DELIVERY_SOURCE_CHANGED"):
        owner_delivery.check_delivery(core.store, core.settings, core.clock, row["message_id"])


def test_no_notice_when_a_reply_already_exists_or_the_job_is_not_failed(core, tmp_path):
    reader_carry(core, tmp_path)
    job_id = authorized_job(core, number=3)
    job = core.store.one("SELECT j.*,e.trace_id,e.conversation_id FROM jobs j "
                         "JOIN events e USING(event_id) WHERE job_id=?", (job_id,))
    with core.store.transaction():
        message_id = enqueue(core.store, job["event_id"], job["conversation_id"], "已有回复",
                             core.clock(), key="failure-test-existing")
        with pytest.raises(CoreError, match="OWNER_DELIVERY_JOB_REQUIRED"):
            owner_delivery.record_failure_notice(
                core.store, core.settings, core.clock, job_id, message_id)
    core.fail_job(job, "INTERNAL_ERROR")
    assert [row["delivery_key"] for row in core.store.rows(
        "SELECT delivery_key FROM outbox WHERE origin_event_id=?", (job["event_id"],))] == [
        "failure-test-existing"]
