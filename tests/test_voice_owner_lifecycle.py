"""STOP and replacement grants retire original jobs without replaying local work."""

import json

import pytest

from companion import owner_authority, voice_authority
from companion.contracts import CoreError
from companion.owner_authority_protocol import AuthorityStop
from companion.voice_protocol import VoiceCleanup, VoiceLeaseRef, VoiceStopRef, VoiceStopRequest
from tests.support.owner_authority import activate
from tests.support.voice_jobs import claim, create_tts, prepared, result


@pytest.mark.parametrize("boundary", ["stop", "replacement", "legacy"])
@pytest.mark.parametrize("started", [False, True])
def test_original_voice_job_retirement_preserves_unknown_and_cleanup(core, monkeypatch,
                                                                   boundary, started):
    jobs, message, _, _ = prepared(core, monkeypatch)
    job = create_tts(core, jobs, message)
    _, claimed = claim(jobs)
    ref = VoiceLeaseRef.from_lease(claimed.lease)
    if started:
        jobs.input("local", ref)
    original = voice_authority.current(core.store, core.settings, core.clock)
    usage = core.store.rows("SELECT * FROM model_usage")
    if boundary == "stop":
        owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id="voice-lifecycle-stop", authority=original.authority.as_dict()))
    elif boundary == "replacement":
        with pytest.raises(CoreError, match="LIVE_TEXT_INITIAL_WORK_MUST_BE_ZERO"):
            activate(core, original.policy, revision=original.authority.revision,
                     operation="voice-lifecycle-active-replacement")
        stopped = owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
            operation_id="voice-lifecycle-stop-before-replacement",
            authority=original.authority.as_dict()))
        changed, _ = activate(core, original.policy, revision=stopped["authority"]["revision"],
                             operation="voice-lifecycle-replacement")
        assert changed["authority"] != original.authority.as_dict()
    else:
        # Historical migration leaves this field empty: current grants cannot backfill it.
        core.store.execute("UPDATE voice_jobs SET authority_json='{}',window_sha256=? "
                           "WHERE job_id=?", ("a" * 64, job["job_id"]))
    assert jobs.status(job["job_id"])["state"] == ("UNKNOWN" if started else "CANCELLED")
    row = core.store.one("SELECT * FROM voice_jobs WHERE job_id=?", (job["job_id"],))
    assert row["input_blob"] is row["result_blob"] is row["lease_json"] is None
    assert json.loads(row["authority_json"]) == {} and row["content_retired"] == 1
    with pytest.raises(CoreError, match="VOICE_LEASE_RETIRED"):
        jobs.submit("local", result(ref))
    stop = VoiceStopRequest(ref=VoiceStopRef.from_lease(claimed.lease), outcome="UNKNOWN",
        resource_state="READY", cleanup_epoch=row["cleanup_epoch"],
        cleanup=VoiceCleanup(cache_removed=True, journal_removed=True, temp_removed=True))
    receipt = jobs.stop("local", stop)
    assert receipt.custody_closed
    assert jobs.stop("local", stop) == receipt
    assert core.store.one("SELECT state FROM voice_jobs")["state"] == (
        "UNKNOWN" if started else "CANCELLED")
    assert core.store.one("SELECT active_job_id FROM voice_node_state")["active_job_id"] is None
    assert core.store.rows("SELECT * FROM model_usage") == usage
