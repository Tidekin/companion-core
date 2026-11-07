"""Voice v2 keeps bounded replay while retaining stopped-execution responsibility."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from threading import Barrier

import httpx
import pytest
from pydantic import SecretStr

from companion.contracts import CoreError
from companion.store import Store
from companion.voice_jobs import VoiceJobs, restore_voice_jobs
from companion.voice_protocol import (
    ROUTE_PREFIX,
    VoiceClaimRequest,
    VoiceCleanup,
    VoiceLeaseRef,
    VoiceRecovery,
    VoiceStopRef,
    VoiceStopRequest,
)
from tests.conftest import run
from tests.support.voice_jobs import TOKEN, create_asr, create_tts, prepared
from tests.support.voice_node_api import app_for


def request(jobs, sequence=None):
    session = jobs.session("local")
    return VoiceClaimRequest(node_id="local", incarnation=session.incarnation,
        sequence=sequence or session.last_sequence + 1, manifest_sha256="b" * 64,
        resource_state="READY")


def test_ten_thousand_idle_claims_keep_one_slot_and_reject_consumed_sequences(core, monkeypatch):
    jobs, _, _, _ = prepared(core, monkeypatch)
    first = request(jobs, 1)
    for sequence in range(1, 10_001):
        current = first.model_copy(update={"sequence": sequence})
        assert jobs.claim("local", current).lease is None
    assert core.store.one("SELECT COUNT(*) n FROM voice_node_state")["n"] == 1
    assert core.store.one("SELECT last_claim_seq FROM voice_node_state")["last_claim_seq"] == 10_000
    assert not core.store.one(
        'SELECT 1 FROM pg_catalog.pg_class WHERE '
        "relnamespace=current_schema()::regnamespace AND relkind IN ('r','p')"
        " AND relname='voice_claims'"
    )
    assert jobs.claim("local", current).lease is None
    with pytest.raises(CoreError, match="VOICE_CLAIM_RETIRED"):
        jobs.claim("local", first)
    with pytest.raises(CoreError, match="VOICE_CLAIM_SEQUENCE_GAP"):
        jobs.claim("local", first.model_copy(update={"sequence": 10_002}))
    with pytest.raises(CoreError, match="VOICE_IDEMPOTENCY_CONFLICT"):
        jobs.claim("local", current.model_copy(update={"resource_state": "QUARANTINED"}))


def test_expiry_is_persisted_across_restart_and_clock_rollback(core, monkeypatch):
    jobs, _, _, _ = prepared(core, monkeypatch)
    first = request(jobs)
    jobs.claim("local", first)
    core.clock.advance(core.settings.voice.job_ttl_seconds + 1)
    assert not jobs.session("local").replay_available
    core.clock.advance(-core.settings.voice.job_ttl_seconds - 1)
    with closing(Store(core.settings.database)) as restarted:
        reader = VoiceJobs(restarted, core.settings, core.clock)
        with pytest.raises(CoreError, match="VOICE_CLAIM_RETIRED"):
            reader.claim("local", first)
        assert reader.claim("local", first.model_copy(update={"sequence": 2})).lease is None


def test_two_connections_same_sequence_allocate_one_identical_lease(core, monkeypatch):
    jobs, message_id, _, _ = prepared(core, monkeypatch)
    create_tts(core, jobs, message_id)
    claim = request(jobs)
    barrier = Barrier(2)

    def allocate():
        store = Store(core.settings.database)
        try:
            barrier.wait()
            return VoiceJobs(store, core.settings, core.clock).claim("local", claim)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: allocate(), range(2)))
    assert results[0] == results[1] and results[0].lease is not None
    assert core.store.one("SELECT COUNT(*) n FROM voice_jobs")["n"] == 1
    assert core.store.one("SELECT last_claim_seq FROM voice_node_state")["last_claim_seq"] == 1


def test_session_expiry_cannot_clear_a_newer_claim_from_another_connection(core, monkeypatch):
    jobs, _, _, _ = prepared(core, monkeypatch)
    first = request(jobs)
    jobs.claim("local", first)
    core.clock.advance(core.settings.voice.job_ttl_seconds + 1)
    transaction = jobs._transaction

    @contextmanager
    def concurrent_claim_before_expiry_write():
        with closing(Store(core.settings.database)) as other:
            VoiceJobs(other, core.settings, core.clock).claim(
                "local", first.model_copy(update={"sequence": 2}))
        with transaction():
            yield

    monkeypatch.setattr(jobs, "_transaction", concurrent_claim_before_expiry_write)
    session = jobs.session("local")
    assert session.last_sequence == 2 and session.replay_available
    assert core.store.one("SELECT claim_response_json FROM voice_node_state")["claim_response_json"]


def test_forget_scrubs_voice_content_but_random_stop_capability_only_closes_custody(
    core, monkeypatch
):
    jobs, message_id, event_id, _ = prepared(core, monkeypatch)
    create_tts(core, jobs, message_id)
    claim = request(jobs)
    lease = jobs.claim("local", claim).lease
    ref = VoiceLeaseRef.from_lease(lease)
    stop = VoiceStopRef.from_lease(lease)
    assert lease.stop_token != lease.lease_token
    with pytest.raises(CoreError, match="VOICE_LEASE_MISMATCH"):
        jobs.input("local", ref.model_copy(update={"lease_token": lease.stop_token}))
    jobs.input("local", ref)
    with core.store.transaction():
        core.ledger.expire_events(core.store, [event_id], core.clock())
    row = core.store.one("SELECT * FROM voice_jobs")
    for field in ("input_blob", "result_blob", "input_sha256", "result_sha256", "lease_json",
                  "source_sha256", "source_input_sha256", "outbox_content_sha256"):
        assert row[field] is None
    assert row["state"] == "UNKNOWN" and row["cleanup_state"] == "PENDING"
    assert lease.input_sha256 not in str(core.store.rows(
        "SELECT safe_detail_json FROM audit_log WHERE action LIKE 'voice.%%'"))
    with pytest.raises(CoreError, match="VOICE_LEASE_RETIRED"):
        jobs.input("local", ref)
    with pytest.raises(CoreError, match="VOICE_CLAIM_RETIRED"):
        jobs.claim("local", claim)
    pending = VoiceStopRequest(ref=stop, resource_state="QUARANTINED", outcome="UNKNOWN",
        cleanup_epoch=row["cleanup_epoch"], cleanup=VoiceCleanup(
            cache_removed=True, journal_removed=True, temp_removed=False))
    status = jobs.stop("local", pending)
    assert not status.custody_closed and not status.cleanup_confirmed
    assert (
        core.store.one("SELECT active_job_id FROM voice_node_state")["active_job_id"]
        == lease.job_id
    )
    completed = pending.model_copy(update={"resource_state": "READY", "cleanup": VoiceCleanup(
        cache_removed=True, journal_removed=True, temp_removed=True)})
    status = jobs.stop("local", completed)
    assert status.custody_closed and status.cleanup_confirmed
    assert jobs.stop("local", completed) == status
    assert core.store.one("SELECT state FROM voice_jobs")["state"] == "UNKNOWN"
    assert core.store.rows("SELECT state FROM model_usage") == [{"state": "SETTLED"}]


def test_restore_changes_incarnation_and_requires_owner_stopped_confirmation(core, monkeypatch):
    jobs, message_id, _, _ = prepared(core, monkeypatch)
    create_tts(core, jobs, message_id)
    claim = request(jobs)
    lease = jobs.claim("local", claim).lease
    jobs.input("local", VoiceLeaseRef.from_lease(lease))
    with core.store.transaction():
        restore_voice_jobs(core.store)
    session = jobs.session("local")
    assert session.incarnation != claim.incarnation and session.recovery_required
    with pytest.raises(CoreError, match="VOICE_CLAIM_EPOCH_RETIRED"):
        jobs.claim("local", claim)
    core.settings.control_token = SecretStr("synthetic-owner-control-" * 3)
    body = VoiceRecovery(incarnation=session.incarnation, operator="synthetic-owner",
        service_instance="synthetic-stopped-instance", confirmation="inflight-execution-stopped")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(core)),
                                     base_url="https://test") as client:
            route = ROUTE_PREFIX + "/nodes/local/recover"
            denied = await client.post(route, json=body.model_dump(),
                                       headers={"Authorization": "Bearer " + TOKEN})
            assert denied.status_code == 401
            recovered = await client.post(route, json=body.model_dump(), headers={
                "Authorization": "Bearer " + core.settings.control_token.get_secret_value()})
            assert recovered.status_code == 200 and not recovered.json()["recovery_required"]
            retired = await client.post("/internal/v1/voice-node/claim", json={},
                                        headers={"Authorization": "Bearer " + TOKEN})
            assert (
                retired.status_code == 410 and retired.json()["error"] == "VOICE_UPGRADE_REQUIRED"
            )

    run(scenario())
    assert core.store.one("SELECT state FROM voice_jobs")["state"] == "UNKNOWN"
    assert core.store.one("SELECT active_job_id FROM voice_node_state")["active_job_id"] is None


@pytest.mark.parametrize("late_state", ["READY", "QUARANTINED"])
def test_old_stop_cannot_change_new_job_resource_state(core, monkeypatch, late_state):
    jobs, message_id, event_id, key = prepared(core, monkeypatch)
    create_tts(core, jobs, message_id)
    lease = jobs.claim("local", request(jobs)).lease
    jobs.input("local", VoiceLeaseRef.from_lease(lease))
    stop = VoiceStopRequest(ref=VoiceStopRef.from_lease(lease), resource_state="READY",
        outcome="CANCELLED", cleanup_epoch=lease.privacy_epoch,
        cleanup=VoiceCleanup(cache_removed=True, journal_removed=True, temp_removed=True))
    assert jobs.stop("local", stop).custody_closed
    new_job = create_asr(core, jobs, event_id, key)
    new_lease = jobs.claim("local", request(jobs)).lease
    jobs.input("local", VoiceLeaseRef.from_lease(new_lease))
    before = core.store.one("SELECT * FROM voice_node_state")
    assert before["active_job_id"] == new_job["job_id"] and before["resource_state"] == "RUNNING"
    assert jobs.stop("local", stop.model_copy(update={"resource_state": late_state})).custody_closed
    assert core.store.one("SELECT * FROM voice_node_state") == before


def test_disabled_node_can_ack_cleanup_only_at_current_epoch_through_http(core, monkeypatch):
    jobs, message_id, event_id, _ = prepared(core, monkeypatch)
    create_tts(core, jobs, message_id)
    lease = jobs.claim("local", request(jobs)).lease
    execution = VoiceLeaseRef.from_lease(lease)
    jobs.input("local", execution)
    with core.store.transaction():
        core.ledger.expire_events(core.store, [event_id], core.clock())
    node = core.settings.voice.nodes["local"].model_copy(update={"enabled": False})
    core.settings.voice = core.settings.voice.model_copy(update={"nodes": {"local": node}})
    cleanup_epoch = core.store.one("SELECT cleanup_epoch FROM voice_jobs")["cleanup_epoch"]
    stop = VoiceStopRequest(ref=VoiceStopRef.from_lease(lease), resource_state="READY",
        outcome="UNKNOWN", cleanup_epoch=cleanup_epoch + 1,
        cleanup=VoiceCleanup(cache_removed=True, journal_removed=True, temp_removed=True))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(core)),
                                     base_url="https://test",
                                     headers={"Authorization": "Bearer " + TOKEN}) as client:
            path = ROUTE_PREFIX + "/jobs/" + lease.job_id
            denied = await client.post(path + "/input", json=execution.model_dump())
            assert denied.status_code == 401
            pending = await client.get(ROUTE_PREFIX + "/nodes/local/cleanup")
            assert pending.status_code == 200 and pending.json()["pending_count"] == 1
            assert lease.input_sha256 not in pending.text
            assert pending.headers["cache-control"] == "no-store"
            stale = await client.post(path + "/stop", json=stop.model_dump())
            assert stale.status_code == 200 and stale.json()["custody_closed"]
            assert not stale.json()["cleanup_confirmed"]
            current = stop.model_copy(update={"cleanup_epoch": stale.json()["cleanup_epoch"]})
            confirmed = await client.post(path + "/stop", json=current.model_dump())
            assert confirmed.status_code == 200 and confirmed.json()["cleanup_confirmed"]
            status = await client.get(ROUTE_PREFIX + "/nodes/local/cleanup")
            assert status.json() == {"pending_count": 0, "jobs": []}

    run(scenario())
    assert core.store.one("SELECT state FROM voice_jobs")["state"] == "UNKNOWN"
