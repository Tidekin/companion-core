"""Workspace-local execution journal and short-lived, non-backup result cache."""

import json
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
from pathlib import Path

from companion.voice_protocol import (
    MAX_RESULT_BODY_BYTES,
    SignedVoiceLease,
    VoiceClaimRequest,
    VoiceCleanup,
    VoiceProtocolError,
    VoiceResult,
    VoiceStopRef,
    canonical_bytes,
)


class NodeState:
    """Single parent process owns this state; provider children never open it."""

    def __init__(self, directory: Path):
        if directory.is_symlink() or self._reparse(directory):
            raise VoiceProtocolError("VOICE_STATE_PATH_INVALID")
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache = self.directory / "result-cache"
        self.cache.mkdir(exist_ok=True, mode=0o700)
        if self.cache.is_symlink() or self._reparse(self.cache):
            raise VoiceProtocolError("VOICE_STATE_PATH_INVALID")
        self._lock = None
        self.db = None

    def __enter__(self):
        try:
            lock_path = self.directory / "node.lock"
            if lock_path.is_symlink() or self._reparse(lock_path):
                raise VoiceProtocolError("VOICE_STATE_PATH_INVALID")
            self._lock = lock_path.open("a+b")
            if self._lock.seek(0, 2) == 0:
                self._lock.write(b"0")
                self._lock.flush()
            self._lock.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.close()
            raise VoiceProtocolError("VOICE_NODE_ALREADY_RUNNING") from None
        try:
            database = self.directory / "journal.sqlite"
            marker = self.directory / "initialized"
            if any(path.is_symlink() or self._reparse(path)
                   for path in (database, marker)):
                raise VoiceProtocolError("VOICE_STATE_PATH_INVALID")
            if database.exists() != marker.exists():
                raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
            new = not database.exists()
            self.db = sqlite3.connect(database)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA secure_delete=ON")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA journal_mode=DELETE")
            if new:
                self.db.executescript(
                    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                    "CREATE TABLE jobs (job_id TEXT NOT NULL, generation INTEGER NOT NULL, "
                    "lease_json TEXT NOT NULL, phase TEXT NOT NULL, result_sha TEXT, "
                    "cache_file TEXT, cache_expires REAL, error_code TEXT, "
                    "PRIMARY KEY(job_id,generation));"
                    "CREATE TABLE recovery_audit (recovery_id TEXT PRIMARY KEY, "
                    "record_json TEXT NOT NULL);"
                )
                self.set_meta("resource", {"state": "READY"})
                self._atomic_write(marker, uuid.uuid4().hex.encode())
            self._migrate()
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
            resource = self.resource()
            if resource.get("state") not in {"READY", "RUNNING", "QUARANTINED"}:
                raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
            # Standard cache-directory marker: backup tools can exclude this entire tree.
            tag = self.cache / "CACHEDIR.TAG"
            if not tag.exists():
                self._atomic_write(tag, b"Signature: 8a477f597d28d172789f06886806bc55\n")
            self.cleanup()
            return self
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            self.close()
            raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED") from None

    @staticmethod
    def _reparse(path):
        try:
            return bool(getattr(path.lstat(), "st_file_attributes", 0)
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))
        except FileNotFoundError:
            return False

    def _migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == 2:
            return
        if version != 0:
            raise VoiceProtocolError("VOICE_STATE_UPGRADE_REQUIRED")
        old = [dict(row) for row in self.db.execute("SELECT * FROM jobs")]
        converted = []
        for job in old:
            lease = json.loads(job["lease_json"])
            ref = VoiceStopRef(job_id=job["job_id"], generation=job["generation"],
                              stop_token=lease.get("stop_token") or lease["lease_token"])
            if job["cache_file"]:
                self._cache_path(job["cache_file"]).unlink(missing_ok=True)
            converted.append((job["job_id"], job["generation"],
                "CLEANUP" if job["phase"] == "TERMINAL" else "UNKNOWN",
                canonical_bytes(ref).decode()))
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("ALTER TABLE jobs RENAME TO jobs_v1")
            self.db.execute("CREATE TABLE jobs (job_id TEXT NOT NULL,generation INTEGER NOT NULL,"
                "lease_json TEXT,phase TEXT NOT NULL,result_sha TEXT,cache_file TEXT,"
                "cache_expires REAL,error_code TEXT,stop_ref_json TEXT NOT NULL,"
                "cleanup_epoch INTEGER NOT NULL DEFAULT 0,"
                "terminal_outcome TEXT,temp_json TEXT,PRIMARY KEY(job_id,generation))")
            self.db.executemany("INSERT INTO jobs(job_id,generation,phase,stop_ref_json,"
                "terminal_outcome,error_code) VALUES (?,?,?,?,'UNKNOWN','VOICE_UPGRADE_REQUIRED')",
                converted)
            self.db.execute("DROP TABLE jobs_v1")
            self.db.execute("DELETE FROM meta WHERE key='pending_claim'")
            self.db.execute("PRAGMA user_version=2")
        self.db.execute("VACUUM")
        resource = self.resource()
        if resource["state"] == "RUNNING":
            self.quarantine("VOICE_UPGRADE_REQUIRES_STOPPED_EXECUTION")

    def bind_session(self, session, *, recovered=False):
        cursor = self.meta("claim_cursor")
        if session.recovery_required:
            raise VoiceProtocolError("VOICE_NODE_RECOVERY_REQUIRED")
        if recovered:
            if (self.active_job() is not None or self.pending_cleanup() is not None
                or self.resource()["state"] != "READY"):
                raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
            with self.db:
                self.db.execute("DELETE FROM meta WHERE key='pending_claim'")
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('claim_cursor',?)",
                    (canonical_bytes({"incarnation": session.incarnation,
                                      "sequence": session.last_sequence}).decode(),))
            return
        if cursor is None:
            if session.last_sequence:
                raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
            self.set_meta("claim_cursor", {"incarnation": session.incarnation, "sequence": 0})
        elif cursor["incarnation"] != session.incarnation:
            raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
        elif cursor["sequence"] > session.last_sequence:
            raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
        elif self.pending_claim() is None and cursor["sequence"] != session.last_sequence:
            raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
        pending = self.pending_claim()
        if pending and (pending.incarnation != session.incarnation
                        or pending.sequence != self.meta("claim_cursor")["sequence"] + 1
                        or session.last_sequence not in {pending.sequence - 1, pending.sequence}):
            raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")

    def prepare_owner_recovery(self):
        if self.resource()["state"] != "READY":
            raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
        rows = [dict(row) for row in self.db.execute("SELECT * FROM jobs")]
        for job in rows:
            if job["lease_json"] is not None:
                self.finish(self.stop_ref(job), outcome=job["terminal_outcome"] or "UNKNOWN",
                            phase="UNKNOWN", error_code="VOICE_OWNER_RECOVERY_REQUESTED")
        report = self.cleanup()
        if report["pending_temp"] or report["unowned_temp"]:
            raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
        for job in rows:
            current = self.job(job["job_id"], job["generation"])
            if not self.cleanup_receipt(current).complete():
                raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
        return [self.stop_ref(job) for job in rows]

    def complete_owner_recovery(self, missing, session, *, node_id, old_cursor,
                                operator, service_instance, confirmation):
        """Retire exact absent refs only after explicit, verified Core owner recovery."""
        expected = {(ref.job_id, ref.generation) for ref in missing}
        report = self.cleanup()
        if (report["pending_temp"] or report["unowned_temp"] or session.recovery_required
            or confirmation != "inflight-execution-stopped"):
            raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = [dict(row) for row in self.db.execute("SELECT * FROM jobs")]
            if (self.resource()["state"] != "READY" or self.meta("claim_cursor") != old_cursor
                or {(row["job_id"], row["generation"]) for row in rows} != expected
                or any(row[field] is not None for row in rows for field in
                       ("lease_json", "result_sha", "cache_file", "cache_expires", "temp_json"))
                or {self.stop_ref(row) for row in rows} != set(missing)):
                raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
            record = {
                "source": "owner_control_recovery", "node_id": node_id,
                "old_cursor": old_cursor,
                "new_cursor": {"incarnation": session.incarnation,
                               "sequence": session.last_sequence},
                "operator": operator, "service_instance": service_instance,
                "confirmation": confirmation, "recorded_at_unix": time.time(),
                "missing_jobs": [{"job_id": row["job_id"], "generation": row["generation"],
                                  "outcome": row["terminal_outcome"],
                                  "core_cleanup_confirmed": False,
                                  "local_cleanup_completed": True} for row in rows],
            }
            self.db.execute("INSERT INTO recovery_audit VALUES (?,?)",
                            ("recovery_" + uuid.uuid4().hex, canonical_bytes(record).decode()))
            self.db.executemany("DELETE FROM jobs WHERE job_id=? AND generation=?", expected)
            self.db.execute("DELETE FROM meta WHERE key='pending_claim'")
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('claim_cursor',?)",
                            (canonical_bytes(record["new_cursor"]).decode(),))

    def next_claim(self, node_id, manifest):
        previous = self.pending_claim()
        if previous:
            return previous
        cursor = self.meta("claim_cursor")
        if not cursor:
            raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
        request = VoiceClaimRequest(node_id=node_id, incarnation=cursor["incarnation"],
            sequence=cursor["sequence"] + 1, manifest_sha256=manifest,
            resource_state=self.resource()["state"])
        self.save_claim(request)
        return request

    def stop_ref(self, job):
        return VoiceStopRef.model_validate_json(job["stop_ref_json"])

    def pending_cleanup(self):
        row = self.db.execute("SELECT * FROM jobs WHERE lease_json IS NULL "
                              "ORDER BY job_id,generation LIMIT 1").fetchone()
        return dict(row) if row else None

    def _owned_temp(self, name):
        if not re.fullmatch(r"[a-f0-9]{32}", name):
            raise VoiceProtocolError("VOICE_TEMP_PATH_INVALID")
        parent = self.directory / "child-temp"
        if parent.is_symlink() or self._reparse(parent):
            raise VoiceProtocolError("VOICE_TEMP_PATH_INVALID")
        path = parent / name
        if (path.is_symlink() or self._reparse(path)
            or not path.resolve().is_relative_to(self.directory)):
            raise VoiceProtocolError("VOICE_TEMP_PATH_INVALID")
        return path

    def prepare_temp(self, lease):
        name = uuid.uuid4().hex
        with self.db:
            self.db.execute("UPDATE jobs SET temp_json=? WHERE job_id=? AND generation=?",
                (canonical_bytes({"name": name, "pid": None, "identity": None}).decode(),
                 lease.job_id, lease.generation))
        return self._owned_temp(name)

    def bind_process(self, lease, pid, identity):
        row = self.job(lease.job_id, lease.generation)
        owned = json.loads(row["temp_json"])
        owned.update(pid=pid, identity=identity)
        with self.db:
            self.db.execute("UPDATE jobs SET temp_json=? WHERE job_id=? AND generation=?",
                            (canonical_bytes(owned).decode(), lease.job_id, lease.generation))

    def process_stopped(self, lease, pid, identity):
        """Record the supervisor's completed wait for precisely this owned local child."""
        job = self.job(lease.job_id, lease.generation)
        if not job or not job["temp_json"]:
            return
        owned = json.loads(job["temp_json"])
        if not pid or not identity or (owned["pid"], owned["identity"]) != (pid, identity):
            return
        owned["stopped"] = True
        with self.db:
            self.db.execute("UPDATE jobs SET temp_json=? WHERE job_id=? AND generation=?",
                            (canonical_bytes(owned).decode(), lease.job_id, lease.generation))

    def _clean_temp(self, job):
        from companion_voice_node.supervisor import process_has_exited

        if not job["temp_json"]:
            return True
        owned = json.loads(job["temp_json"])
        path = self._owned_temp(owned["name"])
        if path.exists():
            if not owned.get("stopped") and (not owned["pid"] or not owned["identity"]
                or not process_has_exited(owned["pid"], owned["identity"])):
                return False
            # Private state ownership plus no-follow traversal; never touch unowned sibling dirs.
            for directory, dirs, files in os.walk(path, followlinks=False):
                for name in dirs + files:
                    entry = Path(directory) / name
                    if entry.is_symlink() or self._reparse(entry):
                        raise VoiceProtocolError("VOICE_TEMP_PATH_INVALID")
            shutil.rmtree(path)
        with self.db:
            self.db.execute("UPDATE jobs SET temp_json=NULL WHERE job_id=? AND generation=?",
                            (job["job_id"], job["generation"]))
        return True

    def cleanup_receipt(self, job):
        temp_removed = self._clean_temp(job)
        parent = self.directory / "child-temp"
        known = {json.loads(row[0])["name"] for row in self.db.execute(
            "SELECT temp_json FROM jobs WHERE temp_json IS NOT NULL")}
        if parent.exists() and any(path.name not in known for path in parent.iterdir()):
            temp_removed = False
        row = self.job(job["job_id"], job["generation"])
        return VoiceCleanup(cache_removed=row["cache_file"] is None,
                            journal_removed=row["lease_json"] is None and row["result_sha"] is None,
                            temp_removed=temp_removed)

    def cleanup(self):
        for row in self.db.execute("SELECT * FROM jobs WHERE lease_json IS NOT NULL").fetchall():
            lease = SignedVoiceLease.model_validate_json(row["lease_json"])
            if row["phase"] == "CLAIMED" and row["temp_json"]:
                # Once ownership has been prepared, a later cleaner must retire
                # this claim before removing its proof; it cannot resume/spawn again.
                self.finish(lease, outcome="CANCELLED", phase="CANCELLED",
                            error_code="VOICE_NODE_RESTARTED_BEFORE_DISPATCH")
            elif lease.job_deadline_at.timestamp() <= time.time():
                phase = "UNKNOWN" if row["phase"] == "RUNNING" else "CANCELLED"
                self.finish(lease, outcome=phase, phase=phase, error_code="VOICE_LEASE_EXPIRED")
        self.prune_cache()
        rows = [dict(row) for row in self.db.execute("SELECT * FROM jobs")]
        pending = sum(not self._clean_temp(row) for row in rows)
        owned = {json.loads(row["temp_json"])["name"] for row in rows if row["temp_json"]}
        parent = self.directory / "child-temp"
        if parent.exists() and (parent.is_symlink() or self._reparse(parent)):
            raise VoiceProtocolError("VOICE_TEMP_PATH_INVALID")
        orphan_count = sum(p.name not in owned for p in parent.iterdir()) if parent.exists() else 0
        return {"pending_temp": pending, "unowned_temp": orphan_count,
                "pending_ack": sum(row["lease_json"] is None for row in rows),
                "core_cleanup_confirmed": False}

    def acknowledge_cleanup(self, job, response):
        with self.db:
            self.db.execute("UPDATE jobs SET cleanup_epoch=? WHERE job_id=? AND generation=?",
                            (response.cleanup_epoch, job["job_id"], job["generation"]))
            if (response.custody_closed and response.cleanup_confirmed
                and response.cleanup_epoch == job["cleanup_epoch"]
                and self.resource()["state"] == "READY"):
                self.db.execute("DELETE FROM jobs WHERE job_id=? AND generation=? "
                                "AND lease_json IS NULL "
                                "AND cache_file IS NULL AND temp_json IS NULL",
                                (job["job_id"], job["generation"]))

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def _atomic_write(self, path: Path, data: bytes):
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".part")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            if os.name != "nt":
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def meta(self, key: str):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key: str, value: dict):
        with self.db:
            self.db.execute(
                "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, canonical_bytes(value).decode()),
            )

    def resource(self) -> dict:
        value = self.meta("resource")
        if not isinstance(value, dict):
            raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
        return value

    def pending_claim(self) -> VoiceClaimRequest | None:
        value = self.meta("pending_claim")
        return VoiceClaimRequest.model_validate(value) if value else None

    def save_claim(self, claim: VoiceClaimRequest):
        previous = self.pending_claim()
        if previous is not None and previous != claim:
            raise VoiceProtocolError("VOICE_CLAIM_REPLAY_MISMATCH")
        self.set_meta("pending_claim", claim.model_dump(mode="json"))

    def clear_claim(self):
        previous = self.pending_claim()
        with self.db:
            if previous:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('claim_cursor',?)",
                    (canonical_bytes({"incarnation": previous.incarnation,
                                      "sequence": previous.sequence}).decode(),))
            self.db.execute("DELETE FROM meta WHERE key='pending_claim'")

    def job(self, job_id: str, generation: int) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM jobs WHERE job_id=? AND generation=?",
            (job_id, generation),
        ).fetchone()
        return dict(row) if row else None

    def active_job(self) -> dict | None:
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE phase NOT IN ('TERMINAL','CLEANUP') LIMIT 2"
        ).fetchall()
        if len(rows) > 1:
            raise VoiceProtocolError("VOICE_STATE_RECOVERY_REQUIRED")
        return dict(rows[0]) if rows else None

    def accept_claim(self, lease: SignedVoiceLease) -> dict:
        with self.db:
            existing = self.job(lease.job_id, lease.generation)
            if existing is None:
                if self.active_job() is not None:
                    raise VoiceProtocolError("VOICE_NODE_BUSY")
                self.db.execute(
                    "INSERT INTO jobs(job_id,generation,lease_json,phase,stop_ref_json,"
                    "cleanup_epoch) "
                    "VALUES (?,?,?,'CLAIMED',?,?)",
                    (lease.job_id, lease.generation, canonical_bytes(lease).decode(),
                     canonical_bytes(VoiceStopRef.from_lease(lease)).decode(), lease.privacy_epoch),
                )
            else:
                previous = SignedVoiceLease.model_validate_json(existing["lease_json"])
                fields = (
                    "node_id",
                    "operation",
                    "lease_token",
                    "input_sha256",
                    "input_bytes",
                    "input_mime",
                    "source_partial",
                    "manifest_sha256",
                    "issued_at",
                    "job_deadline_at",
                    "incarnation", "claim_sequence", "stop_token", "privacy_epoch",
                )
                if any(getattr(previous, name) != getattr(lease, name) for name in fields):
                    raise VoiceProtocolError("VOICE_CLAIM_REPLAY_MISMATCH")
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('claim_cursor',?)",
                (canonical_bytes({"incarnation": lease.incarnation,
                                  "sequence": lease.claim_sequence}).decode(),))
            self.db.execute("DELETE FROM meta WHERE key='pending_claim'")
        return self.job(lease.job_id, lease.generation)

    def update_lease(self, lease: SignedVoiceLease):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET lease_json=? WHERE job_id=? AND generation=?",
                (canonical_bytes(lease).decode(), lease.job_id, lease.generation),
            )

    def phase(self, lease: SignedVoiceLease, phase: str, *, error_code: str | None = None):
        if phase in {"UNKNOWN", "FAILED", "RESULT_LOST", "CANCELLED"}:
            self.finish(lease, error_code=error_code, outcome=phase, phase=phase)
            return
        with self.db:
            self.db.execute(
                "UPDATE jobs SET phase=?,error_code=? WHERE job_id=? AND generation=?",
                (phase, error_code, lease.job_id, lease.generation),
            )

    def dispatch(self, lease: SignedVoiceLease, *, pid: int, process_identity: str):
        if self.resource()["state"] != "READY":
            raise VoiceProtocolError("VOICE_GPU_QUARANTINED")
        resource = {
            "state": "RUNNING",
            "operation": lease.operation,
            "job_id": lease.job_id,
            "generation": lease.generation,
            "pid": pid,
            "process_identity": process_identity,
        }
        with self.db:
            current = self.job(lease.job_id, lease.generation)
            if current is None or current["phase"] != "CLAIMED":
                raise VoiceProtocolError("VOICE_DISPATCH_ALREADY_RECORDED")
            self.db.execute(
                "UPDATE jobs SET phase='RUNNING' WHERE job_id=? AND generation=?",
                (lease.job_id, lease.generation),
            )
            self.db.execute(
                "UPDATE meta SET value=? WHERE key='resource'",
                (canonical_bytes(resource).decode(),),
            )

    def release_resource(self):
        if self.resource()["state"] == "QUARANTINED":
            raise VoiceProtocolError("VOICE_GPU_QUARANTINED")
        self.set_meta("resource", {"state": "READY"})

    def quarantine(self, reason: str):
        resource = self.resource()
        if resource["state"] == "QUARANTINED":
            return
        resource.update(
            state="QUARANTINED",
            quarantine_id="quarantine_" + uuid.uuid4().hex,
            reason=reason,
        )
        self.set_meta("resource", resource)

    def recover_manually(
        self,
        *,
        quarantine_id: str,
        operator: str,
        service_instance: str,
        confirmation: str,
    ):
        resource = self.resource()
        if (
            resource["state"] != "QUARANTINED"
            or resource.get("quarantine_id") != quarantine_id
            or confirmation != "inflight-execution-stopped"
            or not operator.strip()
            or len(operator) > 128
            or not service_instance.strip()
            or len(service_instance) > 128
        ):
            raise VoiceProtocolError("VOICE_RECOVERY_CONFIRMATION_REQUIRED")
        receipt = {
            "source": "manual_operator_confirmation",
            "operator": operator,
            "service_instance": service_instance,
            "quarantine_id": quarantine_id,
            "confirmation": confirmation,
            "recorded_at_unix": time.time(),
        }
        job = self.active_job()
        if job and job["temp_json"]:
            if resource.get("job_id") not in {None, job["job_id"]} or (
                resource.get("generation") not in {None, job["generation"]}
            ):
                raise VoiceProtocolError("VOICE_RECOVERY_CONFIRMATION_REQUIRED")
            owned = json.loads(job["temp_json"])
            receipt.update(job_id=job["job_id"], generation=job["generation"],
                           execution_nonce=owned["name"])
        with self.db:
            if job and job["temp_json"]:
                owned["stopped"] = True
                self.db.execute("UPDATE jobs SET temp_json=? WHERE job_id=? AND generation=?",
                    (canonical_bytes(owned).decode(), job["job_id"], job["generation"]))
            self.db.execute(
                "INSERT INTO recovery_audit VALUES (?,?)",
                ("recovery_" + uuid.uuid4().hex, canonical_bytes(receipt).decode()),
            )
            self.db.execute("UPDATE meta SET value=? WHERE key='resource'", ('{"state":"READY"}',))

    def _cache_path(self, name: str) -> Path:
        if not re.fullmatch(r"voice_[a-f0-9]{32}_[0-9]+(?:_[a-f0-9]{64})?\.json", name):
            raise VoiceProtocolError("VOICE_STATE_PATH_INVALID")
        path = self.cache / name
        if (path.is_symlink() or path.exists() and self._reparse(path)
            or not path.resolve().is_relative_to(self.cache.resolve())):
            raise VoiceProtocolError("VOICE_STATE_PATH_INVALID")
        return path

    def save_result(self, result: VoiceResult, *, expires_at: float):
        from companion_delivery_protocol.singing import MAX_RESULT_BODY_BYTES as song_limit

        data = canonical_bytes(result)
        limit = song_limit if result.operation == "sing" else MAX_RESULT_BODY_BYTES
        if len(data) > limit or expires_at <= time.time():
            raise VoiceProtocolError("VOICE_RESULT_CACHE_EXPIRED")
        name = f"{result.ref.job_id}_{result.ref.generation}.json"
        path = self._cache_path(name)
        existing = self.job(result.ref.job_id, result.ref.generation)
        if existing is None or existing["phase"] not in {"RUNNING", "RESULT_READY"}:
            raise VoiceProtocolError("VOICE_RESULT_STATE_INVALID")
        if existing["result_sha"] not in {None, result.result_sha256}:
            raise VoiceProtocolError("VOICE_RESULT_CONFLICT")
        self._atomic_write(path, data)
        with self.db:
            self.db.execute(
                "UPDATE jobs SET phase='RESULT_READY',result_sha=?,cache_file=?,cache_expires=? "
                "WHERE job_id=? AND generation=?",
                (result.result_sha256, name, expires_at, result.ref.job_id, result.ref.generation),
            )

    def cached_result(self, job: dict) -> VoiceResult | None:
        from companion_delivery_protocol.singing import MAX_RESULT_BODY_BYTES as song_limit

        if not job["cache_file"] or job["cache_expires"] <= time.time():
            return None
        path = self._cache_path(job["cache_file"])
        lease = SignedVoiceLease.model_validate_json(job["lease_json"])
        limit = song_limit if lease.operation == "sing" else MAX_RESULT_BODY_BYTES
        try:
            with path.open("rb") as handle:
                data = handle.read(limit + 1)
        except FileNotFoundError:
            return None
        if len(data) > limit:
            raise VoiceProtocolError("VOICE_RESULT_CACHE_INVALID")
        try:
            result = VoiceResult.model_validate_json(data)
        except ValueError:
            raise VoiceProtocolError("VOICE_RESULT_CACHE_INVALID") from None
        if (
            result.ref.job_id != job["job_id"]
            or result.ref.generation != job["generation"]
            or result.result_sha256 != job["result_sha"]
        ):
            raise VoiceProtocolError("VOICE_RESULT_CACHE_INVALID")
        return result

    def finish(self, lease, *, error_code=None, outcome="COMMITTED", phase="CLEANUP"):
        job = self.job(lease.job_id, lease.generation)
        if job and job["cache_file"]:
            self._cache_path(job["cache_file"]).unlink(missing_ok=True)
        with self.db:
            self.db.execute(
                "UPDATE jobs SET phase=?,lease_json=NULL,result_sha=NULL,cache_file=NULL,"
                "cache_expires=NULL,error_code=?,terminal_outcome=? "
                "WHERE job_id=? AND generation=?",
                (phase, error_code, outcome, lease.job_id, lease.generation),
            )

    def prune_cache(self):
        now = time.time()
        with self.db:
            for row in self.db.execute(
                "SELECT * FROM jobs WHERE cache_file IS NOT NULL AND cache_expires<=?",
                (now,),
            ).fetchall():
                self.finish(self.stop_ref(dict(row)), outcome="RESULT_LOST", phase="RESULT_LOST",
                            error_code="VOICE_RESULT_CACHE_EXPIRED")
        referenced = {
            row[0]
            for row in self.db.execute("SELECT cache_file FROM jobs WHERE cache_file IS NOT NULL")
        }
        for path in self.cache.iterdir():
            if path.name == "CACHEDIR.TAG" or path.name in referenced:
                continue
            if path.is_symlink() or self._reparse(path):
                raise VoiceProtocolError("VOICE_STATE_PATH_INVALID")
            if path.name.endswith(".part") or re.fullmatch(
                r"voice_[a-f0-9]{32}_[0-9]+(?:_[a-f0-9]{64})?\.json",
                path.name,
            ):
                path.unlink()
