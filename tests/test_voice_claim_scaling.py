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
    event.pop("record_sequence")
    template = {key: value for key, value in template.items() if key != "record_sequence"}
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
    # Each cloned voice job must retain its own native event identity.
    assert core.store.one("SELECT count(*) n FROM voice_jobs v JOIN events e USING(event_id) "
                          "WHERE e.source_event_id=v.native_source_event_id")["n"] == count + 1


def voice_tuple_reads(core):
    return core.store.one(
        "SELECT seq_tup_read + idx_tup_fetch AS n FROM pg_stat_xact_user_tables "
        "WHERE schemaname=current_schema() AND relname='voice_jobs'"
    )["n"]


def claim_reads(core, jobs):
    with core.store.transaction():
        before = voice_tuple_reads(core)
        response = claim(jobs, resource_state="QUARANTINED")[1]
        visits = voice_tuple_reads(core) - before
    assert response.lease is None
    assert visits >= 0
    return visits


def test_claim_work_stays_bounded_with_ten_thousand_closed_jobs(core, monkeypatch):
    jobs, _, event_id, key = prepared(core, monkeypatch)
    job = create_asr(core, jobs, event_id, key)
    current = core.store.one('SELECT * FROM voice_jobs WHERE job_id=%s', (job["job_id"],))
    claim_reads(core, jobs)
    without_history = claim_reads(core, jobs)
    archived_asr_rows(core, current, 10_000)
    core.store.execute("ANALYZE voice_jobs")
    # A full scan of the same archive must register all rows in this counter.
    with core.store.read_snapshot():
        before = voice_tuple_reads(core)
        core.store.one("SELECT sum(octet_length(native_source_event_id)) FROM voice_jobs")
        assert voice_tuple_reads(core) - before >= 10_001
    with_history = claim_reads(core, jobs)
    assert with_history <= without_history + 4
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
