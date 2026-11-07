import pytest

from companion import transport
from companion.contracts import CoreError, DeliveryResult
from companion.voice_jobs import VoiceJobs
from companion.voice_protocol import VoiceCleanup, VoiceStopRef, VoiceStopRequest
from tests.support.voice_flow import leased
from tests.support.voice_jobs import TEXT, start
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
