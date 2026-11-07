"""Voice retirement drains through actual worker maintenance without a foreign writer."""

import json
import os
import subprocess
import sys
from pathlib import Path

from pydantic import SecretStr

from companion.store import after, uid
from companion.voice_protocol import VoiceLeaseRef
from tests.support.voice_jobs import claim, control, create_asr, create_tts, prepared, result


def seeds(core, monkeypatch):
    jobs, message_id, event_id, key = prepared(core, monkeypatch)
    completed = create_tts(core, jobs, message_id)
    _, response = claim(jobs)
    ref = VoiceLeaseRef.from_lease(response.lease)
    jobs.input("local", ref)
    audio = result(ref)
    control(jobs, ref, "RESULT_READY", "READY", result_sha256=audio.result_sha256)
    jobs.submit("local", audio)
    running = create_asr(core, jobs, event_id, key)
    _, active = claim(jobs)
    jobs.input("local", VoiceLeaseRef.from_lease(active.lease))
    # There is no runnable chat and no delivery sender in this synthetic worker process.
    core.store.execute("UPDATE jobs SET state='RETRY_WAIT',next_attempt_at=?",
                       (after(core.clock(), 3600),))
    return completed, running


def invariants(core):
    return {table: core.store.rows(f"SELECT * FROM {table} ORDER BY rowid")
            for table in ("outbox", "model_usage", "model_admissions", "cloud_cost_snapshots")}


def launch(core, tmp_path, maintenance, label):
    config, report = tmp_path / "worker-config.json", tmp_path / "worker-report.json"
    config.write_text(json.dumps({"settings": core.settings.model_dump(mode="python")
        | {"memory_enabled": False, "proactive_enabled": False}, "now": core.clock(),
        "maintenance_seconds": maintenance, "report": str(report)},
        default=lambda value: value.get_secret_value() if isinstance(value, SecretStr)
        else str(value)), encoding="utf-8")
    # All fixture/config writes precede launch. Parent only waits/reads from this point.
    child = subprocess.run([sys.executable, "-B", "-m", "tests.voice_wakeup_process", str(config)],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, encoding="utf-8", timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert child.returncode == 0, child.stderr
    value = json.loads(report.read_text(encoding="utf-8"))
    assert value["pid"] != os.getpid() and value["complete"]
    assert value["watchers"] == value["closed_watchers"] == 1
    assert value["provider_calls"] == 0
    assert value["production_maintenance_seconds"] == 60
    assert all(not row["worked"] for row in value["scans"])
    assert all(row["reason"] != "commit" for row in value["waits"])
    destination = os.getenv("VOICE_WAKEUP_RECEIPTS")
    if destination:
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"voice-wakeup-{label}.json").write_text(json.dumps(value, indent=2),
                                                        encoding="utf-8")
    return value


def assert_retired(core, running, before):
    rows = core.store.rows("SELECT * FROM voice_jobs")
    for row in rows:
        assert row["content_retired"] == 1 and row["cleanup_state"] == "PENDING"
        assert all(row[field] is None for field in (
            "input_blob", "result_blob", "input_sha256", "result_sha256", "lease_json",
            "lease_token_sha256", "source_sha256", "source_input_sha256", "source_proof_sha256",
            "window_sha256", "manifest_sha256", "config_sha256", "identity_sha256",
            "outbox_content_sha256"))
        assert row["state"] == ("UNKNOWN" if row["job_id"] == running["job_id"] else "EXPIRED")
    state = core.store.one("SELECT * FROM voice_node_state")
    assert state["active_job_id"] == running["job_id"] and state["resource_state"] == "RUNNING"
    assert state["claim_response_json"] is None
    assert invariants(core) == before


def test_more_than_two_thousand_expired_voice_rows_drain_across_real_maintenance_wait(
    core, tmp_path, monkeypatch,
):
    completed, running = seeds(core, monkeypatch)
    original = core.store.one("SELECT * FROM voice_jobs WHERE job_id=?", (completed["job_id"],))
    outbox = core.store.one("SELECT * FROM outbox WHERE message_id=?", (original["message_id"],))
    # Bulk maintenance fixture copies a natively accepted/completed seed. Unique native
    # message and delivery keys preserve FK/uniqueness; no cloned work is ever dispatched.
    with core.store.transaction():
        for _ in range(2503):
            message = uid("msg")
            item = outbox | {"message_id": message, "delivery_key": "synthetic-bulk:" + message}
            core.store.insert("outbox", **item)
            core.store.insert("voice_jobs", **(original | {"job_id": uid("voice"),
                "message_id": message, "outbox_delivery_key": item["delivery_key"]}))
        core.store.execute("UPDATE voice_jobs SET deadline_at=?,result_expires_at=?,lease_until=?",
                           (after(core.clock(), -1),) * 3)
    assert core.store.rows("PRAGMA foreign_key_check") == []
    before = invariants(core)
    evidence = launch(core, tmp_path, 3.0, "bulk")
    batches = [row["before"] - row["remaining"] for row in evidence["scans"]]
    assert [count for count in batches if count] == [1000, 1000, 505]
    waits = [row for row in evidence["waits"] if row["reason"] == "maintenance"]
    assert waits and waits[0]["returned"] - waits[0]["entered"] > 0.05
    assert evidence["expiry_queries"] == len(evidence["scans"])
    assert_retired(core, running, before)


def test_future_voice_deadline_is_bounded_by_next_real_maintenance_without_writer(
    core, tmp_path, monkeypatch,
):
    _, running = seeds(core, monkeypatch)
    deadline, maintenance = 0.3, 0.8
    core.store.execute("UPDATE voice_jobs SET deadline_at=?,result_expires_at=?,lease_until=?",
                       (after(core.clock(), deadline),) * 3)
    before = invariants(core)
    evidence = launch(core, tmp_path, maintenance, "future")
    assert evidence["clock_origin"] == "first_native_scan"
    retired = next(row for row in evidence["scans"] if row["remaining"] == 0)
    assert evidence["scans"][0]["remaining"] == 2
    waits = [row for row in evidence["waits"] if row["reason"] == "maintenance"]
    assert waits and any(row["entered"] < deadline < row["returned"] for row in waits)
    assert deadline <= retired["ended"]
    scan_time = max(row["ended"] - row["started"] for row in evidence["scans"])
    assert retired["ended"] - deadline <= maintenance + scan_time + 0.25
    assert_retired(core, running, before)
