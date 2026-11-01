"""Strict assertions for the sole permitted output of an authorized failed chat."""

import json

from companion import owner_delivery


def assert_failure_notice(core, event_id=None):
    rows = core.store.rows("SELECT * FROM outbox" + (
        " WHERE origin_event_id=?" if event_id is not None else ""),
        (event_id,) if event_id is not None else ())
    assert len(rows) == 1, rows
    row = rows[0]
    material = json.loads(core.store.one("SELECT value FROM runtime_meta WHERE key=?",
        (owner_delivery.DELIVERY + row["message_id"],))["value"])
    job = core.store.one("SELECT * FROM jobs WHERE job_id=?", (material["job_id"],))
    assert material["kind"] == owner_delivery.FAILURE_NOTICE
    assert material["failure_code"] == job["last_error_code"]
    assert job["kind"] == "chat" and job["state"] == "FAILED"
    assert row["origin_event_id"] == job["event_id"]
    assert row["state"] == "PENDING" and row["action_type"] == "qq.private.reply"
    assert row["delivery_key"] == f"failure:{job['job_id']}:v1"
    assert json.loads(row["content_json"]) == {
        "text": owner_delivery.failure_text(job["last_error_code"])}
    assert json.loads(row["authority_json"]) == json.loads(job["authority_json"])
    assert owner_delivery.check_delivery(
        core.store, core.settings, core.clock, row["message_id"])["approved"] is True
    return row
