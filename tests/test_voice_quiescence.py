"""Stopped execution can finish custody while its original outcome stays UNKNOWN."""

import pytest

from companion import owner_input, owner_input_state, transport
from companion.contracts import CoreError, DeliveryResult
from companion.voice_jobs import VoiceJobs
from companion.voice_protocol import VoiceCleanup, VoiceStopRef, VoiceStopRequest
from tests.test_owner_input import captured, inbound
from tests.test_voice_flow import leased
from tests.test_voice_jobs import TEXT, start
from tests.voice_owner_fixtures import native_reply


def stopped_unknown(core):
    """Actual node input release, token-bound stop/cleanup and original text ACK."""
    message = native_reply(core, TEXT)
    jobs = VoiceJobs(core.store, core.settings, core.clock)
    job, _, claimed, ref = start(core, jobs, message)
    row = core.store.one("SELECT * FROM voice_jobs WHERE job_id=?", (job["job_id"],))
    stopped = jobs.stop("local", VoiceStopRequest(
        ref=VoiceStopRef.from_lease(claimed.lease), resource_state="READY", outcome="UNKNOWN",
        cleanup_epoch=row["cleanup_epoch"], cleanup=VoiceCleanup(
            cache_removed=True, journal_removed=True, temp_removed=True)))
    assert stopped.custody_closed and stopped.cleanup_confirmed
    item = leased(core, message)
    transport.begin_send(core.store, message, item["lease_token"], core.clock(),
                         core.settings, clock=core.clock)
    transport.delivery_result(core.store, core.settings, message, DeliveryResult(
        lease_token=item["lease_token"], result="SENT", provider_message_id="synthetic-stop-ack"),
        core.clock())
    with pytest.raises(CoreError, match="VOICE_LEASE_RETIRED"):
        jobs.input("local", ref)
    return core.store.one("SELECT * FROM voice_jobs WHERE job_id=?", (job["job_id"],))


def receipt_fixture(core, tmp_path):
    capture, _ = captured(core, tmp_path)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    accepted = core.ingest(inbound(core, capture), "closed-voice-source")
    core.store.execute("UPDATE jobs SET state='CANCELLED' WHERE job_id=?", (accepted["job_id"],))
    event = core.store.one("SELECT * FROM events WHERE event_id=?", (capture.event_id,))
    now = core.clock()
    core.store.insert("voice_jobs", job_id="voice-stopped", operation="asr",
        event_id=capture.event_id, native_source_event_id=event["source_event_id"],
        conversation_id=event["conversation_id"], source_segment_index=0,
        captured_privacy_epoch=0, captured_reply_revision=event["reply_revision"],
        node_id="local", config_json="{}", state="UNKNOWN", input_mime="audio/wav",
        source_partial=0, created_at=now, updated_at=now, deadline_at=now,
        content_retired=1, cleanup_state="CONFIRMED", cleanup_confirmed_at=now,
        stop_token_sha256="a" * 64, authority_json="{}")
    core.store.insert("voice_node_state", node_id="local", resource_state="READY",
                      last_seen_at=now)
    return capture


@pytest.mark.parametrize("missing", [None, "retirement", "cleanup", "cleanup_receipt",
    "stop_token", "lease", "lease_token", "node", "active_job", "claim_job",
    "recovery", "quarantine", "node_error", "QUEUED", "LEASED", "RUNNING"])
def test_only_proven_stopped_unknown_is_quiescent_without_rewriting_it(core, tmp_path, missing):
    capture = receipt_fixture(core, tmp_path)
    job_changes = {"retirement": ("content_retired", 0), "cleanup": ("cleanup_state", "PENDING"),
        "cleanup_receipt": ("cleanup_confirmed_at", None),
        "stop_token": ("stop_token_sha256", None),
        "lease": ("lease_json", "{}"), "lease_token": ("lease_token_sha256", "b" * 64)}
    node_changes = {"active_job": ("active_job_id", "voice-stopped"),
        "claim_job": ("claim_job_id", "voice-stopped"), "recovery": ("recovery_required", 1),
        "quarantine": ("resource_state", "QUARANTINED"), "node_error": ("last_error_code", "LOST")}
    if missing in job_changes:
        field, value = job_changes[missing]
        core.store.execute(f"UPDATE voice_jobs SET {field}=?", (value,))
    elif missing in node_changes:
        field, value = node_changes[missing]
        core.store.execute(f"UPDATE voice_node_state SET {field}=?", (value,))
    elif missing == "node":
        core.store.execute("DELETE FROM voice_node_state")
    elif missing in {"QUEUED", "LEASED", "RUNNING"}:
        core.store.execute("UPDATE voice_jobs SET state=?", (missing,))
    before = list(core.store.db.iterdump())
    result = owner_input_state.read(core.store, core.settings, capture)
    assert result["unsettled_work"] is bool(missing)
    assert result["terminal"] is (missing is None)
    assert list(core.store.db.iterdump()) == before


def test_pending_restore_requires_existing_native_node_recovery_evidence(core, tmp_path):
    capture = receipt_fixture(core, tmp_path)
    core.store.execute("INSERT OR REPLACE INTO runtime_meta VALUES ('voice_restore_required','1')")
    assert owner_input_state.read(core.store, core.settings, capture)["unsettled_work"]
    # Same proof as VoiceJobs.session: the current incarnation must be recovered.
    core.store.execute("UPDATE voice_node_state SET recovered_incarnation=(SELECT value "
                       "FROM runtime_meta WHERE key='voice_incarnation')")
    before = list(core.store.db.iterdump())
    assert owner_input_state.read(core.store, core.settings, capture)["terminal"]
    assert list(core.store.db.iterdump()) == before
