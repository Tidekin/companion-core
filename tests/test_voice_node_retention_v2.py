"""Retired execution data is removed while content-free custody stays replayable."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from companion.voice_protocol import (
    SignedVoiceLease,
    VoiceProtocolError,
    VoiceSession,
    VoiceStopResponse,
    digest,
)
from companion_voice_node.state import NodeState
from companion_voice_node.supervisor import process_has_exited, process_identity
from companion_voice_node.worker import VoiceNodeWorker, local_manifest, main
from tests.test_voice_node_worker import (
    FakeCore,
    FixtureSupervisor,
    child_receipts,
    factory,
    write_config,
)
from tests.test_voice_node_worker import node as node  # noqa: F401 -- shared fixture


def scope(node):
    config, key, _ = node
    remote = FakeCore(config, key)
    lease = SignedVoiceLease.model_validate(remote.lease(digest(local_manifest(config))))
    return config, remote, lease


def session(incarnation="b" * 32, sequence=0, recovery=False):
    return VoiceSession(incarnation=incarnation, last_sequence=sequence,
                        replay_available=bool(sequence), recovery_required=recovery)


@pytest.mark.parametrize("failure", ["newer_core", "older_core", "incarnation", "recovery"])
def test_claim_cursor_never_silently_adopts_rollback_or_restore(node, failure):
    config, _, _ = node
    with NodeState(config.state_directory) as state:
        state.bind_session(session())
        state.next_claim(config.node_id, "a" * 64)
        state.clear_claim()
        before = state.meta("claim_cursor")
        changed = {
            "newer_core": session(sequence=2), "older_core": session(sequence=0),
            "incarnation": session(incarnation="c" * 32, sequence=1),
            "recovery": session(sequence=1, recovery=True),
        }[failure]
        with pytest.raises(VoiceProtocolError, match="RECOVERY_REQUIRED"):
            state.bind_session(changed)
        assert state.meta("claim_cursor") == before


def test_pending_claim_allows_only_exact_unacknowledged_successor(node):
    config, _, _ = node
    with NodeState(config.state_directory) as state:
        state.bind_session(session())
        pending = state.next_claim(config.node_id, "a" * 64)
        state.bind_session(session(sequence=1))
        assert state.next_claim(config.node_id, "a" * 64) == pending
        with pytest.raises(VoiceProtocolError, match="RECOVERY_REQUIRED"):
            state.bind_session(session(sequence=2))
        assert state.pending_claim() == pending and state.meta("claim_cursor")["sequence"] == 0


def test_missing_entire_journal_cannot_adopt_existing_core_sequence(node):
    config, _, _ = node
    with NodeState(config.state_directory) as state:
        with pytest.raises(VoiceProtocolError, match="RECOVERY_REQUIRED"):
            state.bind_session(session(sequence=4))
        assert state.meta("claim_cursor") is None


def test_cleanup_epoch_and_local_quarantine_gate_detail_removal(node):
    config, _, lease = scope(node)
    with NodeState(config.state_directory) as state:
        state.accept_claim(lease)
        state.quarantine("SYNTHETIC_UNKNOWN")
        state.phase(lease, "UNKNOWN")
        job = state.active_job()
        state.acknowledge_cleanup(job, VoiceStopResponse(
            custody_closed=True, cleanup_confirmed=True, cleanup_epoch=0,
        ))
        assert state.active_job()["stop_ref_json"] == job["stop_ref_json"]
        state.recover_manually(quarantine_id=state.resource()["quarantine_id"],
            operator="synthetic", service_instance="confirmed-stopped",
            confirmation="inflight-execution-stopped")
        state.acknowledge_cleanup(job, VoiceStopResponse(
            custody_closed=True, cleanup_confirmed=True, cleanup_epoch=1,
        ))
        job = state.active_job()
        assert job["cleanup_epoch"] == 1
        state.acknowledge_cleanup(job, VoiceStopResponse(
            custody_closed=True, cleanup_confirmed=True, cleanup_epoch=1,
        ))
        assert state.active_job() is None


@pytest.mark.parametrize("loss", ["before", "after"])
def test_stop_ack_loss_and_new_epoch_keep_ref_but_never_content(node, loss):
    config, remote, lease = scope(node)
    remote.cleanup_epoch = 3
    with NodeState(config.state_directory) as state:
        state.accept_claim(lease)
        state.phase(lease, "CANCELLED")
    lost = False

    def transport(request):
        nonlocal lost
        assert request.url.path.endswith("/stop")
        if not lost and loss == "before":
            lost = True
            raise httpx.ReadError("synthetic loss before stop")
        answer = remote(request)
        if not lost and loss == "after":
            lost = True
            raise httpx.ReadError("synthetic loss after stop")
        return answer

    async def scenario():
        worker = VoiceNodeWorker(config, transport=httpx.MockTransport(transport))
        with pytest.raises(VoiceProtocolError, match="VOICE_CORE_UNAVAILABLE"):
            await worker.run(once=True)
        with NodeState(config.state_directory) as state:
            row = state.active_job()
            assert row["lease_json"] is None and row["result_sha"] is None
            assert state.stop_ref(row).stop_token == lease.stop_token
        assert await worker.run(once=True) == "DISCARDED"
        assert remote.stops[-1].cleanup_epoch == 3
        assert remote.stops[-1].cleanup.complete()
        assert all(row.ref.stop_token != lease.lease_token for row in remote.stops)
        assert child_receipts(node[2]) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("ownership", ["prepared", "live", "exited", "unknown_sibling"])
def test_cleanup_only_removes_exact_proven_stopped_temp(node, ownership, monkeypatch):
    config, _, lease = scope(node)
    with NodeState(config.state_directory) as state:
        state.accept_claim(lease)
        path = state.prepare_temp(lease)
        path.mkdir(parents=True)
        sentinel = path / "private.txt"
        sentinel.write_text("synthetic private temp")
        if ownership != "prepared":
            state.bind_process(lease, os.getpid(), process_identity(os.getpid()))
        if ownership == "exited":
            monkeypatch.setattr("companion_voice_node.supervisor.process_has_exited",
                                lambda *_: True)
        if ownership == "unknown_sibling":
            orphan = path.parent / "legacy-unmapped"
            orphan.mkdir()
            (orphan / "keep.txt").write_text("unattributed")
        state.phase(lease, "CANCELLED")
        report = state.cleanup()
        assert sentinel.exists() is (ownership != "exited")
        assert report["pending_temp"] == int(ownership != "exited")
        assert report["unowned_temp"] == int(ownership == "unknown_sibling")
        assert report["pending_ack"] == 1 and report["core_cleanup_confirmed"] is False
        assert state.active_job() is not None


def test_cleanup_only_cli_is_offline_exclusive_and_reports_pending(node, tmp_path, monkeypatch,
                                                                 capsys):
    config, _, lease = scope(node)
    with NodeState(config.state_directory) as state:
        state.accept_claim(lease)
        state.phase(lease, "UNKNOWN")
    path = tmp_path / "node.json"
    write_config(path, config)

    def forbidden(*_args, **_kwargs):
        pytest.fail("cleanup-only must not access network, manifest, or provider")

    monkeypatch.setattr("companion_voice_node.worker.local_manifest", forbidden)
    monkeypatch.setattr("companion_voice_node.worker.httpx.AsyncClient", forbidden)
    assert main(["--config", str(path), "--cleanup-only"], supervisor_factory=forbidden) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["pending_ack"] == 1 and report["core_cleanup_confirmed"] is False
    with NodeState(config.state_directory):
        assert main(["--config", str(path), "--cleanup-only"]) == 1
        assert "VOICE_NODE_ALREADY_RUNNING" in capsys.readouterr().err


@pytest.mark.parametrize("phase", ["TERMINAL", "RUNNING", "UNKNOWN"])
def test_v1_upgrade_scrubs_managed_content_but_preserves_stop_custody(node, phase):
    config, _, lease = scope(node)
    directory = config.state_directory
    directory.mkdir()
    cache = directory / "result-cache"
    cache.mkdir()
    filename = f"{lease.job_id}_1_{lease.input_sha256}.json"
    sentinel = "synthetic-retained-private-transcript"
    (cache / filename).write_text(sentinel)
    (directory / "initialized").write_text("legacy-instance")
    old = lease.model_dump(mode="json")
    for field in ("incarnation", "claim_sequence", "privacy_epoch", "stop_token"):
        del old[field]
    old["schema_version"] = 1
    with sqlite3.connect(directory / "journal.sqlite") as db:
        db.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
            "CREATE TABLE jobs(job_id TEXT,generation INTEGER,lease_json TEXT,phase TEXT,"
            "result_sha TEXT,cache_file TEXT,cache_expires REAL,error_code TEXT,"
            "PRIMARY KEY(job_id,generation));"
            "CREATE TABLE recovery_audit(recovery_id TEXT PRIMARY KEY,record_json TEXT);")
        db.execute("INSERT INTO meta VALUES ('resource',?)", (json.dumps({
            "state": "READY" if phase == "TERMINAL" else "QUARANTINED",
            "quarantine_id": "legacy-quarantine",
        }),))
        db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,NULL)",
                   (lease.job_id, 1, json.dumps(old), phase, lease.input_sha256, filename,
                    time.time() + 120))
    with NodeState(directory) as state:
        job = state.job(lease.job_id, 1)
        assert job["lease_json"] is None and job["cache_file"] is None
        assert state.stop_ref(job).stop_token == lease.lease_token
        assert job["phase"] == ("CLEANUP" if phase == "TERMINAL" else "UNKNOWN")
        assert state.resource()["state"] == (
            "READY" if phase == "TERMINAL" else "QUARANTINED")
        assert state.cleanup()["pending_ack"] == 1
        assert state.db.execute("PRAGMA secure_delete").fetchone()[0] == 1
    for file in directory.rglob("*"):
        if file.is_file():
            data = file.read_bytes()
            assert sentinel.encode() not in data
            assert lease.input_sha256.encode() not in data
            assert filename.encode() not in data


def test_independent_cleanup_only_cli_preserves_offline_pending(node, tmp_path):
    config, _, lease = scope(node)
    with NodeState(config.state_directory) as state:
        state.accept_claim(lease)
        state.phase(lease, "UNKNOWN")
    path = tmp_path / "cleanup-node.json"
    write_config(path, config)
    completed = subprocess.run([sys.executable, "-B", "-m", "companion_voice_node.worker",
        "--config", str(path), "--cleanup-only"], cwd=config.workspace,
        capture_output=True, text=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["pending_ack"] == 1
    with NodeState(config.state_directory) as state:
        assert state.active_job()["phase"] == "UNKNOWN"


def test_retired_live_result_purges_before_stop_and_never_reexecutes(node):
    config, remote, _ = scope(node)
    counter = node[2]
    stopped = []

    def transport(request):
        if request.url.path.endswith("/result"):
            return httpx.Response(410, json={"error": "VOICE_LEASE_RETIRED"})
        if request.url.path.endswith("/stop"):
            assert not list((config.state_directory / "result-cache").glob("voice_*.json"))
            stopped.append(json.loads(request.content))
        return remote(request)

    worker = VoiceNodeWorker(config, transport=httpx.MockTransport(transport),
                             supervisor_factory=factory(counter))
    assert asyncio.run(worker.run(once=True)) == "DISCARDED"
    assert len(child_receipts(counter)) == 1 and stopped[-1]["cleanup"]["journal_removed"]
    assert asyncio.run(worker.run(once=True)) == "IDLE"
    assert len(child_receipts(counter)) == 1


def test_owner_claim_recovery_holds_local_lock_before_network(node, tmp_path, capsys):
    config, _, _ = node
    path, token = tmp_path / "node.json", tmp_path / "owner-token"
    write_config(path, config)
    token.write_text("synthetic-owner-token")

    def forbidden(request):
        pytest.fail("locked state cannot send owner recovery")

    with NodeState(config.state_directory):
        assert main(["--config", str(path), "--recover-claims", "--control-token-file",
            str(token), "--operator", "synthetic", "--service-instance", "stopped",
            "--confirmation", "inflight-execution-stopped"],
            transport=httpx.MockTransport(forbidden)) == 1
        assert "VOICE_NODE_ALREADY_RUNNING" in capsys.readouterr().err


def test_explicit_claim_recovery_retires_old_pending_identity_atomically(node):
    config, _, _ = node
    with NodeState(config.state_directory) as state:
        state.bind_session(session())
        state.next_claim(config.node_id, "a" * 64)
        state.bind_session(session(incarnation="c" * 32, sequence=9), recovered=True)
        assert state.pending_claim() is None
        following = state.next_claim(config.node_id, "a" * 64)
        assert following.incarnation == "c" * 32 and following.sequence == 10


@pytest.mark.parametrize("failure", [None, "wrong_token", "owner_loss", "generic404",
    "invalid404", "stop_timeout", "changed_incarnation", "changed_sequence", "bad_session",
    "quarantined", "unowned_temp", "pending_temp", "before_commit", "transaction_failure",
    "stop_401", "stop_403", "stop_409", "stop_410", "stop_500",
    "owner_incarnation", "owner_recovery_required"])
def test_explicit_owner_recovery_only_retires_exact_observed_missing_refs(node, failure,
                                                                       monkeypatch):
    config, remote, lease = scope(node)
    calls = []
    session_calls = 0
    cursor = {"incarnation": lease.incarnation, "sequence": lease.claim_sequence}
    recovered = session(incarnation="c" * 32, sequence=7).model_dump()

    def transport(request):
        nonlocal session_calls
        calls.append(request.url.path)
        if request.url.path.endswith("/session"):
            session_calls += 1
            value = dict(recovered)
            if session_calls == 2 and failure == "changed_incarnation":
                value["incarnation"] = "d" * 32
            if session_calls == 2 and failure == "changed_sequence":
                value["last_sequence"] += 1
            return httpx.Response(200, json=value)
        if request.url.path.endswith("/recover"):
            assert request.headers["authorization"] == "Bearer synthetic-owner-token"
            if failure == "wrong_token":
                return httpx.Response(401)
            if failure == "owner_loss":
                raise httpx.ReadError("synthetic owner ACK lost")
            if failure == "owner_incarnation":
                return httpx.Response(200, json=recovered | {"incarnation": "d" * 32})
            if failure == "owner_recovery_required":
                return httpx.Response(200, json=recovered | {"recovery_required": True})
            return httpx.Response(200, json={} if failure == "bad_session" else recovered)
        assert request.url.path.endswith("/stop")
        assert request.headers["authorization"] == (
            "Bearer " + config.credential.get_secret_value())
        if failure == "stop_timeout":
            raise httpx.ReadTimeout("synthetic stop timeout")
        if failure and failure.startswith("stop_"):
            return httpx.Response(int(failure.removeprefix("stop_")), json={"error": "REJECTED"})
        if failure == "invalid404":
            return httpx.Response(404, content=b"{broken")
        return httpx.Response(404, json={"error": (
            "OTHER_NOT_FOUND" if failure == "generic404" else "VOICE_JOB_NOT_FOUND")})

    with NodeState(config.state_directory) as state:
        state.accept_claim(lease)
        if failure == "quarantined":
            state.quarantine("SYNTHETIC_UNCONFIRMED")
        if failure == "unowned_temp":
            (config.state_directory / "child-temp" / "unowned").mkdir(parents=True)
        if failure == "pending_temp":
            state.prepare_temp(lease).mkdir(parents=True)
        state.phase(lease, "UNKNOWN")
        complete = state.complete_owner_recovery
        if failure == "before_commit":
            def fail_before_commit(*args, **kwargs):
                raise VoiceProtocolError("SYNTHETIC_CRASH_BEFORE_COMMIT")
            monkeypatch.setattr(state, "complete_owner_recovery", fail_before_commit)
        if failure == "transaction_failure":
            state.db.execute("CREATE TRIGGER fail_recovery_cursor BEFORE INSERT ON meta "
                "WHEN NEW.key='claim_cursor' BEGIN SELECT RAISE(ABORT,'synthetic crash'); END")
        worker = VoiceNodeWorker(config, transport=httpx.MockTransport(transport))
        if failure:
            with pytest.raises((VoiceProtocolError, sqlite3.IntegrityError)):
                asyncio.run(worker.recover_claims(state, owner_token="synthetic-owner-token",
                    operator="owner", service_instance="stopped-instance",
                    confirmation="inflight-execution-stopped"))
            assert state.stop_ref(state.active_job()).stop_token == lease.stop_token
            assert state.meta("claim_cursor") == cursor
            assert state.db.execute("SELECT COUNT(*) FROM recovery_audit").fetchone()[0] == 0
            if failure in {"quarantined", "unowned_temp", "pending_temp"}:
                assert calls == []
            if failure in {"owner_loss", "before_commit", "transaction_failure"}:
                if failure == "transaction_failure":
                    state.db.execute("DROP TRIGGER fail_recovery_cursor")
                monkeypatch.setattr(state, "complete_owner_recovery", complete)
                failure = None
                asyncio.run(worker.recover_claims(state, owner_token="synthetic-owner-token",
                    operator="owner", service_instance="stopped-instance",
                    confirmation="inflight-execution-stopped"))
                assert state.active_job() is None
                assert state.meta("claim_cursor")["sequence"] == 7
        else:
            asyncio.run(worker.recover_claims(state, owner_token="synthetic-owner-token",
                operator="owner", service_instance="stopped-instance",
                confirmation="inflight-execution-stopped"))
            assert state.active_job() is None
            assert state.meta("claim_cursor") == {"incarnation": "c" * 32, "sequence": 7}
            record = json.loads(state.db.execute("SELECT record_json FROM recovery_audit")
                                .fetchone()[0])
            assert record["missing_jobs"] == [{"job_id": lease.job_id, "generation": 1,
                "outcome": "UNKNOWN", "core_cleanup_confirmed": False,
                "local_cleanup_completed": True}]
            assert record["node_id"] == config.node_id and record["operator"] == "owner"
            encoded = json.dumps(record)
            assert lease.stop_token not in encoded and lease.input_sha256 not in encoded
            assert state.next_claim(config.node_id, "a" * 64).sequence == 8
    assert child_receipts(node[2]) == [] and remote.result_bodies == []


def test_ordinary_worker_native_missing_job_never_counts_as_cleanup_ack(node):
    config, _, lease = scope(node)
    with NodeState(config.state_directory) as state:
        state.accept_claim(lease)
        state.phase(lease, "UNKNOWN")
    worker = VoiceNodeWorker(config, transport=httpx.MockTransport(
        lambda _: httpx.Response(404, json={"error": "VOICE_JOB_NOT_FOUND"})))
    with pytest.raises(VoiceProtocolError, match="VOICE_CORE_UNAVAILABLE"):
        asyncio.run(worker.run(once=True))
    with NodeState(config.state_directory) as state:
        assert state.active_job()["phase"] == "UNKNOWN"
        assert state.meta("claim_cursor")["sequence"] == lease.claim_sequence


@pytest.mark.parametrize("missing", [True, False])
def test_real_core_restore_owner_recovery_missing_or_existing_unknown(core, node, tmp_path,
                                                                   monkeypatch, missing):
    from cryptography.fernet import Fernet

    from companion.backup import backup, restore
    from companion.core import Core
    from companion.voice_jobs import VoiceJobs
    from companion.voice_protocol import VoiceLeaseRef
    from tests.support.voice_jobs import TOKEN, claim, create_tts, prepared
    from tests.test_voice_node_api import app_for

    jobs, message, _, _ = prepared(core, monkeypatch)
    archive, restored_path = tmp_path / "early.enc", tmp_path / "restored.db"
    key = Fernet.generate_key()
    if missing:
        backup(core.settings.database, archive, key)
    created = create_tts(core, jobs, message)
    _, answer = claim(jobs)
    lease = answer.lease
    jobs.input("local", VoiceLeaseRef.from_lease(lease))
    if not missing:
        backup(core.settings.database, archive, key)
    restore(archive, restored_path, key, core.settings.erasure_ledger)
    config = node[0].model_copy(update={"node_id": "local",
        "credential": type(node[0].credential)(TOKEN)})
    settings = core.settings.model_copy(update={"database": str(restored_path)})
    settings.control_token = type(config.credential)("synthetic-owner-control-" * 3)
    with Core(settings, core.model, core.clock) as restored:
        restored_jobs = VoiceJobs(restored.store, settings, core.clock)
        with NodeState(config.state_directory) as state:
            state.accept_claim(lease)
            state.phase(lease, "UNKNOWN")
            worker = VoiceNodeWorker(config, transport=httpx.ASGITransport(app=app_for(restored)))
            asyncio.run(worker.recover_claims(state,
                owner_token=settings.control_token.get_secret_value(), operator="synthetic-owner",
                service_instance="restored-stopped-instance",
                confirmation="inflight-execution-stopped"))
            assert state.active_job() is None
            current = restored_jobs.session("local")
            assert state.meta("claim_cursor")["incarnation"] == current.incarnation
            assert current.incarnation != lease.incarnation and not current.recovery_required
            record = json.loads(state.db.execute("SELECT record_json FROM recovery_audit")
                                .fetchone()[0])
            assert len(record["missing_jobs"]) == int(missing)
            if missing:
                assert not record["missing_jobs"][0]["core_cleanup_confirmed"]
            else:
                retained = restored.store.one("SELECT * FROM voice_jobs WHERE job_id=?",
                                              (created["job_id"],))
                assert retained["state"] == "UNKNOWN"
                assert retained["cleanup_state"] == "CONFIRMED"
            audit = restored.store.one(
                "SELECT * FROM audit_log WHERE action='voice.owner_recovered'",
            )
            detail = json.loads(audit["safe_detail_json"])
            assert audit["entity_id"] == "local"
            assert detail["incarnation"] == current.incarnation
            assert detail["operator"] == record["operator"] == "synthetic-owner"
            assert detail["service_instance"] == record["service_instance"]
            assert restored.store.rows("SELECT state FROM model_usage") == [{"state": "SETTLED"}]
            following = state.next_claim("local", "b" * 64)
            assert following.sequence == current.last_sequence + 1

            async def claim_after_recovery():
                from companion.voice_protocol import ROUTE_PREFIX

                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(restored)),
                                             base_url="https://core.invalid") as client:
                    reply = await client.post(ROUTE_PREFIX + "/claim",
                        json=following.model_dump(), headers={"Authorization": "Bearer " + TOKEN,
                            "Idempotency-Key": following.request_id})
                    assert reply.status_code == 403, reply.text
                    assert reply.json() == {"error": "OWNER_AUTHORITY_DISABLED"}

            asyncio.run(claim_after_recovery())
            state.clear_claim()
            assert restored_jobs.session("local").last_sequence == current.last_sequence


def test_process_identity_is_durable_before_ready_wait_and_startup_failure_cleanup(node):
    config, remote, _ = scope(node)
    observations = []

    class StartupFailure(FixtureSupervisor):
        async def _read(self):
            with sqlite3.connect(config.state_directory / "journal.sqlite") as db:
                owned = json.loads(db.execute("SELECT temp_json FROM jobs").fetchone()[0])
            assert owned["pid"] == self.process.pid and owned["identity"] == self.identity
            observations.append(owned)
            raise VoiceProtocolError("VOICE_PROCESS_START_FAILED")

    worker = VoiceNodeWorker(config, transport=httpx.MockTransport(remote),
        supervisor_factory=lambda workspace, temporary: StartupFailure(
            workspace, temporary, counter=node[2]))
    with pytest.raises(VoiceProtocolError, match="VOICE_PROCESS_START_FAILED"):
        asyncio.run(worker.run(once=True))
    assert len(observations) == 1 and child_receipts(node[2]) == []
    with NodeState(config.state_directory) as state:
        assert state.active_job()["phase"] == "CANCELLED"
        assert state.active_job()["temp_json"] is None
        assert state.resource()["state"] == "READY"
    assert asyncio.run(worker.run(once=True)) == "DISCARDED"


@pytest.mark.parametrize("target_exists", [True, False])
def test_substituted_temp_link_never_deletes_target_or_acknowledges_cleanup(node, tmp_path,
                                                                        target_exists):
    config, _, lease = scope(node)
    target = tmp_path / "other-owned-data"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("unrelated private data")
    with NodeState(config.state_directory) as state:
        state.accept_claim(lease)
        path = state.prepare_temp(lease)
        path.parent.mkdir(exist_ok=True)
        state.phase(lease, "CANCELLED")
        if os.name == "nt":
            created = subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(target)],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW)
            assert created.returncode == 0, created.stderr
        else:
            path.symlink_to(target, target_is_directory=True)
        if not target_exists:
            sentinel.unlink()
            target.rmdir()
        try:
            with pytest.raises(VoiceProtocolError, match="VOICE_TEMP_PATH_INVALID"):
                state.cleanup()
            assert state.active_job()["temp_json"] is not None
            if target_exists:
                assert sentinel.read_text() == "unrelated private data"
        finally:
            # Remove only this test-created link itself, never its target.
            if os.name == "nt":
                path.rmdir()
            else:
                path.unlink()


@pytest.mark.parametrize("initialized", [False, True])
@pytest.mark.parametrize("name", ["node.lock", "journal.sqlite", "initialized"])
@pytest.mark.parametrize("link_kind", ["file", "junction", "stat_boundary"])
def test_dangling_state_file_link_never_creates_external_target(
    node, tmp_path, initialized, name, link_kind, monkeypatch,
):
    config, _, _ = node
    if initialized:
        with NodeState(config.state_directory):
            pass
    else:
        config.state_directory.mkdir()
    path = config.state_directory / name
    path.unlink(missing_ok=True)
    outside = tmp_path / "outside-state"
    outside.mkdir()
    target = outside / name
    if link_kind == "junction":
        if os.name != "nt":
            pytest.skip("Windows native reparse fixture")
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(target)],
            capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stderr
        assert NodeState._reparse(path)
    elif link_kind == "stat_boundary":
        original = type(path).is_symlink
        monkeypatch.setattr(type(path), "is_symlink",
                            lambda current: current == path or original(current))
    else:
        try:
            path.symlink_to(target)
        except OSError as error:
            if os.name == "nt" and error.winerror == 1314:
                pytest.skip("File symlink needs Windows privilege; stat/junction run locally")
            raise
        assert path.is_symlink()
    assert not path.exists() and not target.exists()
    try:
        with pytest.raises(VoiceProtocolError,
                           match="VOICE_STATE_(PATH_INVALID|RECOVERY_REQUIRED)"):
            with NodeState(config.state_directory):
                pytest.fail("dangling state link must be rejected before open")
        assert not target.exists() and list(outside.iterdir()) == []
        assert path.is_symlink() or NodeState._reparse(path)
    finally:
        if link_kind == "junction":
            path.rmdir()
        elif link_kind == "file":
            path.unlink()


def test_absent_ordinary_state_files_still_initialize_and_reopen(node):
    config, _, _ = node
    assert not config.state_directory.exists()
    with NodeState(config.state_directory) as state:
        state.set_meta("synthetic_marker", {"value": "retained"})
    with NodeState(config.state_directory) as state:
        assert state.meta("synthetic_marker") == {"value": "retained"}
        assert state.resource()["state"] == "READY"


async def temp_crash_fixture(config_path, lease_path, counter, stage):
    from companion_voice_node.worker import VoiceNodeConfig

    config = VoiceNodeConfig.model_validate_json(
        await asyncio.to_thread(Path(config_path).read_bytes),
    )
    lease = SignedVoiceLease.model_validate_json(
        await asyncio.to_thread(Path(lease_path).read_bytes),
    )
    with NodeState(config.state_directory) as state:
        state.accept_claim(lease)
        temporary = state.prepare_temp(lease)
        temporary.mkdir(parents=True)
        (temporary / "private.txt").write_text("synthetic crash residue")
        child = None
        if stage != "prepared":
            child = FixtureSupervisor(config.workspace, temporary, counter=Path(counter))
            await child.start(5)
            if stage != "spawned":
                state.bind_process(lease, child.process.pid, child.identity)
            if stage == "stopped":
                assert await child.stop()
                state.process_stopped(lease, child.process.pid, child.identity)
        print(json.dumps({"stage": stage, "name": temporary.name,
                          "pid": child.process.pid if child else None,
                          "identity": child.identity if child else None}), flush=True)
        await asyncio.Event().wait()


@pytest.mark.parametrize("stage", ["prepared", "spawned", "bound", "stopped"])
def test_real_parent_crash_before_dispatch_never_overwrites_temp_ownership(node, tmp_path, stage):
    config, remote, lease = scope(node)
    config_path, lease_path = tmp_path / "node.json", tmp_path / "lease.json"
    write_config(config_path, config)
    lease_path.write_text(lease.model_dump_json())

    async def scenario():
        process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
            "tests.test_voice_node_retention_v2", "--temp-crash", str(config_path),
            str(lease_path), str(node[2]), stage, cwd=config.workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            line = await asyncio.wait_for(process.stdout.readline(), 10)
            assert line, (await process.stderr.read()).decode()
            checkpoint = json.loads(line)
            process.kill()
            await process.wait()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        path = config.state_directory / "child-temp" / checkpoint["name"]
        if checkpoint["pid"]:
            def wait_for_owned_exit():
                deadline = time.monotonic() + 5
                while not process_has_exited(checkpoint["pid"], checkpoint["identity"]):
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
            await asyncio.to_thread(wait_for_owned_exit)
        worker = VoiceNodeWorker(config, transport=httpx.MockTransport(remote),
                                 supervisor_factory=lambda *_: pytest.fail("cannot redispatch"))
        uncertain = stage in {"prepared", "spawned"}
        assert await worker.run(once=True) == ("RETRY" if uncertain else "DISCARDED")
        with NodeState(config.state_directory) as state:
            if uncertain:
                assert path.exists() and state.active_job()["phase"] == "CANCELLED"
                assert state.resource()["state"] == "QUARANTINED"
                state.recover_manually(quarantine_id=state.resource()["quarantine_id"],
                    operator="synthetic", service_instance="confirmed-stopped-child",
                    confirmation="inflight-execution-stopped")
            else:
                assert not path.exists() and state.active_job() is None
        if uncertain:
            assert await worker.run(once=True) == "DISCARDED"
            assert not path.exists()
        assert child_receipts(node[2]) == [] and remote.claim_bodies == []

    asyncio.run(scenario())


if __name__ == "__main__" and sys.argv[1:2] == ["--temp-crash"]:
    asyncio.run(temp_crash_fixture(*sys.argv[2:]))
