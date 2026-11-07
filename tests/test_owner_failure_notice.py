"""A chat reply that finally fails under OwnerAuthority tells the owner, with Core wording."""

import json

import pytest

from companion import owner_authority, owner_delivery, paid_budget
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from companion.transport import enqueue
from tests.owner_model_fixtures import authorized_job
from tests.support.carried_capabilities import reader_carry
from tests.support.failure_notice import assert_failure_notice


def failed(core, tmp_path, code):
    reader_carry(core, tmp_path)
    job_id = authorized_job(core, number=3)
    job = core.store.one((
        'SELECT j.*,e.trace_id,e.conversation_id FROM jobs j JOIN events e '
        'USING(event_id) WHERE job_id=%s'
    ), (job_id,))
    core.fail_job(job, code)
    assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (job_id,))["state"] == "FAILED"
    return job


@pytest.mark.parametrize("code,text", [
    ("INTERNAL_ERROR", owner_delivery.FAILURE_TEXT),
    ("SOURCE_GRANT_REQUIRED", owner_delivery.FAILURE_TEXTS["SOURCE_GRANT_REQUIRED"]),
])
def test_final_failure_queues_an_authority_bound_core_notice(core, tmp_path, code, text):
    job = failed(core, tmp_path, code)
    row = core.store.one('SELECT * FROM outbox WHERE origin_event_id=%s', (job["event_id"],))
    assert json.loads(row["content_json"]) == {"text": text}
    assert row["state"] == "PENDING" and row["delivery_key"] == f"failure:{job['job_id']}:v1"
    assert json.loads(row["authority_json"]) == json.loads(job["authority_json"])
    assert owner_delivery.check_delivery(
        core.store, core.settings, core.clock, row["message_id"])["approved"] is True
    # Nothing but the fixed wording can ride on this binding.
    core.store.execute('UPDATE outbox SET content_json=%s WHERE message_id=%s',
                       (json.dumps({"text": "别的内容"}), row["message_id"]))
    with pytest.raises(CoreError, match="OWNER_DELIVERY_SOURCE_CHANGED"):
        owner_delivery.check_delivery(core.store, core.settings, core.clock, row["message_id"])


def test_no_notice_when_a_reply_already_exists_or_the_job_is_not_failed(core, tmp_path):
    reader_carry(core, tmp_path)
    job_id = authorized_job(core, number=3)
    job = core.store.one((
        'SELECT j.*,e.trace_id,e.conversation_id FROM jobs j JOIN events e '
        'USING(event_id) WHERE job_id=%s'
    ), (job_id,))
    with core.store.transaction():
        message_id = enqueue(core.store, job["event_id"], job["conversation_id"], "已有回复",
                             core.clock(), key="failure-test-existing")
        with pytest.raises(CoreError, match="OWNER_DELIVERY_JOB_REQUIRED"):
            owner_delivery.record_failure_notice(
                core.store, core.settings, core.clock, job_id, message_id)
    core.fail_job(job, "INTERNAL_ERROR")
    assert [row["delivery_key"] for row in core.store.rows(
        'SELECT delivery_key FROM outbox WHERE origin_event_id=%s', (job["event_id"],))] == [
        "failure-test-existing"]


@pytest.mark.parametrize("boundary", ["state", "code", "content", "source", "stop", "erase"])
def test_failure_notice_rechecks_job_wording_source_and_authority(core, tmp_path, boundary):
    job = failed(core, tmp_path, "INTERNAL_ERROR")
    row = assert_failure_notice(core)
    if boundary == "state":
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=%s", (job["job_id"],))
    elif boundary == "code":
        core.store.execute((
            "UPDATE jobs SET last_error_code='SOURCE_GRANT_REQUIRED' WHERE "
            'job_id=%s'
        ),
                           (job["job_id"],))
    elif boundary == "content":
        # Even a coherent hash cannot turn fixed Core wording into a model reply.
        content = {"text": "Unapproved model output"}
        core.store.execute((
            'UPDATE outbox SET content_json=%s,content_sha256=%s WHERE '
            'message_id=%s'
        ),
                           (json.dumps(content), paid_budget.sha(content), row["message_id"]))
    elif boundary == "source":
        event = core.store.one('SELECT body_json FROM events WHERE event_id=%s', (job["event_id"],))
        body = json.loads(event["body_json"])
        body["text"] = "Changed owner input"
        core.store.execute('UPDATE events SET body_json=%s WHERE event_id=%s',
                           (json.dumps(body), job["event_id"]))
    elif boundary == "stop":
        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id="stop-failure-notice", authority=json.loads(job["authority_json"])))
    else:
        with core.store.transaction():
            core.ledger.expire_events(core.store, [job["event_id"]], core.clock())
    with pytest.raises(CoreError):
        owner_delivery.check_delivery(core.store, core.settings, core.clock, row["message_id"])


def test_failure_notice_is_idempotent_and_binding_rejection_rolls_back(core, tmp_path, monkeypatch):
    def refuse(*_args):
        raise CoreError("OWNER_SOURCE_CHANGED", 403)

    with monkeypatch.context() as patch:
        patch.setattr(owner_delivery, "record_failure_notice", refuse)
        failed(core, tmp_path, "INTERNAL_ERROR")
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.store.rows("SELECT * FROM runtime_meta WHERE key LIKE 'owner_delivery:%%'") == []
    job_id = authorized_job(core, number=4)
    job = core.store.one((
        'SELECT j.*,e.trace_id,e.conversation_id FROM jobs j JOIN events e '
        'USING(event_id) WHERE job_id=%s'
    ), (job_id,))
    core.fail_job(job, "INTERNAL_ERROR")
    row = assert_failure_notice(core)
    core.fail_job(job, "INTERNAL_ERROR")
    assert assert_failure_notice(core)["message_id"] == row["message_id"]
