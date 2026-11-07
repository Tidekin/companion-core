"""Core maintenance expires voice content even when no Node reconnects."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from companion.contracts import CoreError
from companion.store import after, encode
from companion.voice_protocol import VoiceLeaseRef
from tests.support.voice_jobs import claim, control, create_asr, create_tts, prepared, result
from tests.test_voice_flow import flow_fixture, upload


def test_chat_claim_after_voice_expiry_uses_unavailable_fallback(core, monkeypatch):
    flow, jobs, _, event_id, key = flow_fixture(
        core, monkeypatch, make_reply=False, job_ttl_seconds=5)
    assert flow.input_status(event_id).state == "WAITING"
    upload(core, flow, event_id, key)
    _, active = claim(jobs)
    jobs.input("local", VoiceLeaseRef.from_lease(active.lease))
    core.clock.advance(6)
    chat = core.claim_job("expired-voice-chat")
    assert chat is not None
    context = flow.for_context(event_id, chat)
    evidence = json.loads(context["message"]["content"].split("\n", 1)[1])
    assert evidence["status"] == "UNAVAILABLE" and evidence["transcripts"] == []
    assert core.store.one("SELECT state FROM voice_jobs")["state"] == "UNKNOWN"


def test_expired_voice_projection_is_read_only_and_source_erasure_still_blocks(core, monkeypatch):
    flow, jobs, _, event_id, key = flow_fixture(
        core, monkeypatch, make_reply=False, job_ttl_seconds=5)
    flow.input_status(event_id)
    upload(core, flow, event_id, key)
    core.clock.advance(6)
    with core.store.transaction():
        core.recover(core.clock())
    before = core.store.db.total_changes
    projection = flow.input_receipt(event_id)
    assert projection["state"] == "FALLBACK"
    assert projection["records"][0]["input_sha256"] is None
    assert not projection["records"][0]["upload_allowed"]
    assert core.store.db.total_changes == before
    with core.store.transaction():
        core.ledger.expire_events(core.store, [event_id], core.clock())
    with pytest.raises(CoreError):
        flow.input_receipt(event_id)


def test_independent_chat_worker_scrubs_offline_voice_but_keeps_unknown_custody(
    core, tmp_path, monkeypatch,
):
    jobs, message_id, event_id, key = prepared(core, monkeypatch)
    completed = create_tts(core, jobs, message_id)
    _, claimed = claim(jobs)
    ref = VoiceLeaseRef.from_lease(claimed.lease)
    jobs.input("local", ref)
    audio = result(ref)
    control(jobs, ref, "RESULT_READY", "READY", result_sha256=audio.result_sha256)
    jobs.submit("local", audio)
    running = create_asr(core, jobs, event_id, key)
    _, active = claim(jobs)
    jobs.input("local", VoiceLeaseRef.from_lease(active.lease))
    before = core.store.rows("SELECT * FROM voice_jobs ORDER BY job_id")
    assert any(row["input_blob"] for row in before) and any(row["result_blob"] for row in before)
    state = core.store.one("SELECT * FROM voice_node_state")
    assert state["active_job_id"] == running["job_id"] and state["claim_response_json"]
    outbox = core.store.rows("SELECT * FROM outbox")
    charges = core.store.rows("SELECT * FROM model_usage")
    # Only maintenance is due; the native chat job remains a future retry.
    core.store.execute("UPDATE jobs SET state='RETRY_WAIT',next_attempt_at=?",
                       (after(core.clock(), 3600),))
    core.clock.advance(core.settings.voice.job_ttl_seconds + 1)
    settings = core.settings.model_dump(mode="python") | {"memory_enabled": False}
    config = tmp_path / "expiry-worker.json"
    config.write_text(json.dumps({"settings": settings, "now": core.clock()},
        default=lambda value: value.get_secret_value() if isinstance(value, SecretStr)
        else str(value)), encoding="utf-8")
    child = subprocess.run(
        [sys.executable, "-B", "-m", "tests.voice_expiry_process", str(config)],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, encoding="utf-8",
        timeout=45, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    report = json.loads(child.stdout)
    assert report["pid"] != os.getpid() and report["worked"] is False
    assert report["provider_calls"] == []
    rows = core.store.rows("SELECT * FROM voice_jobs ORDER BY job_id")
    for row in rows:
        assert row["content_retired"] == 1 and row["cleanup_state"] == "PENDING"
        for field in ("input_blob", "result_blob", "input_sha256", "result_sha256",
                      "source_sha256", "source_input_sha256", "source_proof_sha256",
                      "identity_sha256", "outbox_content_sha256", "window_sha256",
                      "manifest_sha256", "config_sha256", "lease_json", "lease_token_sha256"):
            assert row[field] is None
        assert row["state"] == ("UNKNOWN" if row["job_id"] == running["job_id"] else "EXPIRED")
        assert row["job_id"] in {running["job_id"], completed["job_id"]}
    after_state = core.store.one("SELECT * FROM voice_node_state")
    assert after_state["active_job_id"] == running["job_id"]
    assert after_state["resource_state"] == "RUNNING"
    assert after_state["last_claim_seq"] == state["last_claim_seq"]
    for field in ("claim_response_json", "claim_request_sha256",
                  "claim_job_id", "claim_expires_at"):
        assert after_state[field] is None
    audit = encode(core.store.rows("SELECT safe_detail_json FROM audit_log "
                                   "WHERE action IN ('voice.created','voice.committed')"))
    for row in before:
        assert row["input_sha256"] not in audit
        assert not row["result_sha256"] or row["result_sha256"] not in audit
    assert core.store.rows("SELECT * FROM outbox") == outbox
    assert core.store.rows("SELECT * FROM model_usage") == charges
