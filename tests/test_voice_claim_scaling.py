"""Claim work follows live custody, not the volume of archived voice jobs."""

import pytest

from companion import private_life
from companion.contracts import CoreError
from companion.voice_protocol import VoiceLeaseRef
from tests.support.voice_jobs import (
    claim,
    control,
    create_asr,
    create_tts,
    prepared,
    result,
)


def archived_asr_rows(core, template, count):
    # Bulk, schema-valid terminal history; active jobs still use native source guards.
    event = core.store.one('SELECT * FROM events WHERE event_id=%s', (template["event_id"],))
    with core.store.transaction():
        for number in range(count):
            event_id = f"evt_archived_voice_{number}"
            archived_event = event | {
                "event_id": event_id,
                "source_event_id": event["source_event_id"] + f":archive:{number}",
                "idempotency_key": event_id,
            }
            core.store.insert("events", **archived_event)
            core.store.insert("voice_jobs", **(template | {
                "job_id": f"voice_{number:032x}", "event_id": event_id,
                "native_source_event_id": archived_event["source_event_id"],
                "source_sha256": private_life.event_digest(archived_event),
                "state": ("CANCELLED", "EXPIRED", "FAILED")[number % 3], "input_blob": None,
            }))
    assert core.store.rows("PRAGMA foreign_key_check") == []


def claim_steps(core, jobs):
    steps = 0

    def progress():
        nonlocal steps
        steps += 1
        return 0

    core.store.db.set_progress_handler(progress, 1)
    try:
        response = claim(jobs, resource_state="QUARANTINED")[1]
    finally:
        core.store.db.set_progress_handler(None, 0)
    assert response.lease is None
    return steps


def test_claim_sqlite_work_stays_bounded_with_ten_thousand_closed_jobs(core, monkeypatch):
    jobs, _, event_id, key = prepared(core, monkeypatch)
    job = create_asr(core, jobs, event_id, key)
    current = core.store.one('SELECT * FROM voice_jobs WHERE job_id=%s', (job["job_id"],))
    claim_steps(core, jobs)
    without_history = claim_steps(core, jobs)
    archived_asr_rows(core, current, 10_000)
    with_history = claim_steps(core, jobs)
    # A VM instruction count is deterministic across host load and catches historical scans.
    assert 0 < with_history <= without_history + 100
    assert core.store.one('SELECT * FROM voice_jobs WHERE job_id=%s', (job["job_id"],)) == current
    assert core.store.one("SELECT COUNT(*) AS n FROM voice_jobs")["n"] == 10_001
    assert core.store.one("SELECT active_job_id FROM voice_node_state")["active_job_id"] is None


@pytest.mark.parametrize("operation", ["asr", "tts"])
def test_claim_expires_completed_results_before_replaying_old_request(
    core, monkeypatch, operation,
):
    jobs, message_id, event_id, key = prepared(core, monkeypatch, result_ttl_seconds=5)
    job = (create_asr(core, jobs, event_id, key) if operation == "asr"
           else create_tts(core, jobs, message_id))
    request, leased = claim(jobs)
    ref = VoiceLeaseRef.from_lease(leased.lease)
    jobs.input("local", ref)
    value = result(ref, operation=operation)
    control(jobs, ref, "RESULT_READY", "READY", result_sha256=value.result_sha256)
    jobs.submit("local", value)
    core.clock.advance(6)
    with pytest.raises(CoreError, match="VOICE_CLAIM_RETIRED"):
        jobs.claim("local", request)
    assert core.store.one('SELECT state,input_blob,result_blob FROM voice_jobs WHERE job_id=%s',
                          (job["job_id"],)) == {
        "state": "EXPIRED", "input_blob": None, "result_blob": None,
    }
    assert core.store.one("SELECT active_job_id FROM voice_node_state")["active_job_id"] is None
