import json
import os
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config


def uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def utcnow():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def after(now, seconds):
    return (datetime.fromisoformat(now) + timedelta(seconds=seconds)).isoformat(
        timespec="microseconds"
    )


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def migrate(path, revision="head"):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    migrations = Path(__file__).parent.parent / "migrations"
    if not migrations.exists():
        migrations = Path(__file__).parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations))
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + Path(path).resolve().as_posix())
    command.upgrade(cfg, revision)


def file_identity(path):
    """Device, inode, size and mtime of a database and its WAL; every commit changes one."""
    files = []
    for name in (path, str(path) + "-wal"):
        try:
            info = os.stat(name)
        except OSError:
            files.append(None)
        else:
            files.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
    return tuple(files)


def _lock_report(site, started, locked, finished, outcome):
    """Content-free timing of one write transaction, reported only when it is slow."""
    from companion.telemetry import LOCK_REPORT_MS, emit_lock

    wait_ms = (locked - started) * 1000
    held_ms = 0 if finished is None else (finished - locked) * 1000
    if wait_ms + held_ms < LOCK_REPORT_MS:
        return
    code, line = site
    try:
        emit_lock(
            f"{Path(code.co_filename).stem}:{code.co_name}:{line}",
            wait_ms=wait_ms,
            held_ms=held_ms,
            outcome=outcome,
        )
    except Exception:
        pass  # Instrumentation must not replace the transaction's result or exception.


class Store:
    def __init__(self, path):
        self.path = path
        self._budget_stops = None
        # File identity when the current read snapshot began; None outside one.
        self._snapshot_files = None
        # Counts write transactions and read snapshots; identifies the current one.
        self._scope = 0
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        try:
            self.db.row_factory = sqlite3.Row
            for pragma in (
                "foreign_keys=ON",
                "busy_timeout=5000",
                "journal_mode=WAL",
                "synchronous=FULL",
                "secure_delete=ON",
            ):
                self.db.execute("PRAGMA " + pragma)
        except BaseException:
            try:
                self.db.close()
            except Exception:
                pass  # Initialization's original failure remains authoritative.
            raise

    @property
    def in_write_transaction(self):
        """Whether this Store owns an active BEGIN IMMEDIATE scope."""
        return self._budget_stops is not None and self.db.in_transaction

    @property
    def in_read_snapshot(self):
        """Whether this Store is inside `read_or_write`'s query-only snapshot."""
        return self._snapshot_files is not None and self.db.in_transaction

    @property
    def frozen_scope(self):
        """The current write transaction or read snapshot, in which no other writer's
        commit is visible; None outside both."""
        return self._scope if self.in_write_transaction or self.in_read_snapshot else None

    def read_or_write(self, operation):
        """Run a read-only operation on one WAL snapshot instead of the single writer lock.

        WAL readers never block the writer, and the snapshot is a committed state from
        within this call. query_only refuses every write statement, including a nested
        BEGIN IMMEDIATE. A refused write, an external journal ahead of the snapshot, or
        any other failure repeats the operation once under BEGIN IMMEDIATE; that
        outcome, success or error, is the authoritative one.
        """
        if self.db.in_transaction:
            return operation()
        caller = sys._getframe(1)
        try:
            self.db.execute("PRAGMA query_only=ON")
            try:
                self.db.execute("BEGIN")
                # Taken before the first read pins the snapshot: equal identities later
                # mean no commit has happened since the snapshot began.
                self._snapshot_files = file_identity(self.path)
                self._txn_changes = self.db.total_changes
                self._scope += 1
                return operation()
            finally:
                self._snapshot_files = None
                if self.db.in_transaction:
                    self.db.rollback()
                self.db.execute("PRAGMA query_only=OFF")
        except Exception:
            pass  # The locked attempt below decides the result.
        with self.transaction(site=(caller.f_code, caller.f_lineno)):
            return operation()

    @contextmanager
    def transaction(self, *, site=None):
        if site is None:
            caller = sys._getframe(2)  # 0: this generator, 1: contextlib, 2: the `with` site
            site = (caller.f_code, caller.f_lineno)
        started = time.perf_counter()
        try:
            self.db.execute("BEGIN IMMEDIATE")
        except BaseException:
            _lock_report(site, started, time.perf_counter(), None, "BUSY")
            raise
        locked = time.perf_counter()
        self._budget_stops = set()
        self._scope += 1
        # Lets shared caches tell whether this connection has uncommitted writes.
        self._txn_changes = self.db.total_changes
        outcome = "ROLLBACK"
        try:
            yield self
            self.db.commit()
            outcome = "COMMIT"
        except BaseException:
            self.db.rollback()
            if self._budget_stops:
                # A fsynced Budget pause must outlive an unrelated business
                # rollback. Only append-only stop witnesses are replayed here.
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    self.db.executemany(
                        "INSERT OR IGNORE INTO runtime_meta VALUES (?,?)", self._budget_stops
                    )
                    self.db.commit()
                except BaseException:
                    self.db.rollback()
                    raise
            raise
        finally:
            self._budget_stops = None
            _lock_report(site, started, locked, time.perf_counter(), outcome)

    def retain_budget_stop(self, journal_id, head_sha256):
        value = ("model_budget_stop:" + head_sha256, journal_id)
        if self._budget_stops is not None:
            self._budget_stops.add(value)
        self.execute("INSERT OR IGNORE INTO runtime_meta VALUES (?,?)", value)

    def execute(self, sql, params=()):
        return self.db.execute(sql, params)

    def rows(self, sql, params=()):
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def schema_capabilities(self):
        required = {
            "conversations": {"conversation_id", "reply_revision"},
            "events": {"event_id", "redacted", "memory_processed", "fingerprint", "reply_revision"},
            "jobs": {"lease_token", "state", "context_tracked", "authority_json"},
            "job_sources": {"job_id", "event_id"},
            "rem_records": {
                "rem_id",
                "job_id",
                "source_snapshot_json",
                "output_json",
                "source_event_ids_json",
                "redacted",
                "expires_at",
            },
            "agent_conversations": {
                "job_id",
                "phase",
                "conversation_json",
                "snapshots_json",
                "request_id",
                "output_text",
            },
            "agent_tool_calls": {"job_id", "tool_call_id", "state", "request_id", "voice_job_id"},
            "captured_images": {"item_id", "proof_json", "mime", "expires_at"},
            "agent_image_inputs": {"job_id", "item_id", "position"},
            "voice_jobs": {"job_id", "state", "generation", "authority_json",
                           "singing_context_json", "reserved_output_bytes", "source_budget_at",
                           "source_reserved_requests", "source_reserved_bytes",
                           "source_used_requests", "source_used_bytes"},
            "meme_discovery_reservations": {
                "job_id",
                "attempt_key",
                "state",
                "reserved_queries",
                "reserved_download_bytes",
                "reserved_storage_bytes",
            },
            "meme_captions": {
                "job_id",
                "request_sha256",
                "asset_data",
                "source_json",
                "usage_request_id",
                "result_json",
                "reuse_job_id",
            },
            "outbox": {
                "lease_token",
                "state",
                "content_json",
                "reply_group_id",
                "segment_index",
                "segment_count",
                "reply_group_sha256",
                "segment_interval_ms",
            },
            "memories": {"needs_review", "fact_key"},
            "memory_vectors": {"model_version", "content_hash", "vector"},
            "runtime_meta": {"key", "value"},
            "model_admissions": {
                "payload_sha256",
                "snapshot_json",
                "measurement_json",
                "authority_json",
            },
            "owner_authority_revisions": {
                "grant_id",
                "revision",
                "policy_json",
                "policy_sha256",
                "record_sha256",
                "operation_id",
                "request_sha256",
            },
            "diary_runs": {"captured_privacy_epoch", "lease_token", "source_set_sha256"},
            "diary_entries": {"body_md", "status", "version"},
            "diary_evidence": {"event_id", "outbox_message_id"},
            "diary_revisions": {"prior_title", "prior_body_md"},
            "observations": {"event_id", "result_status", "captured_privacy_epoch"},
            "event_dependencies": {"event_id", "source_event_id"},
            "source_grants": {"grant_id", "adapter_id", "revoked_at", "forgotten_at"},
            "source_grant_revisions": {"grant_id", "epoch", "fingerprint", "expires_at"},
            "source_grant_events": {"event_id", "grant_id", "grant_epoch"},
            "consents": {"capabilities_json", "expires_at", "revoked_at"},
            "affect_states": {"state_json", "expires_at", "version"},
            "affect_evidence": {"event_id", "reason_code", "expires_at"},
            "reflection_runs": {
                "captured_privacy_epoch",
                "lease_token",
                "model_request_id",
                "next_attempt_at",
            },
            "reflection_evidence": {"event_id", "outbox_message_id"},
            "reflection_suggestions": {"kind", "content", "state"},
            "consolidation_runs": {"steps_json", "lease_token", "captured_privacy_epoch"},
            "consolidation_evidence": {"event_id", "run_id"},
            "goals": {"scope_json", "state", "redacted", "reflection_suggestion_id"},
            "mood_appraisals": {
                "event_id", "reason", "quote_sha256", "request_id", "target_message_id"
            },
            "goal_evidence": {"event_id", "goal_id"},
            "goal_steps": {
                "lease_token",
                "captured_privacy_epoch",
                "attempts",
                "position",
                "scope_json",
            },
            "goal_step_evidence": {"step_id", "event_id"},
            "commitments": {"deadline", "feedback_message_id", "feedback_outcome"},
            "wake_decisions": {"idempotency_key", "decision", "budget_snapshot"},
            "exploration_requests": {
                "lease_token",
                "reserved_bytes",
                "captured_privacy_epoch",
                "reserved_requests",
                "used_requests",
                "source_grant_id",
                "source_grant_epoch",
            },
            "reading_attempts": {
                "request_id",
                "lease_token",
                "context_json",
                "state",
                "observed_requests",
                "observed_bytes",
            },
            "social_candidates": {"event_id", "message_id", "comparison_text"},
            "tool_tasks": {"scope_sha256", "captured_privacy_epoch", "billed_micro_usd"},
            "tool_approvals": {"scope_sha256", "expires_at", "revoked_at"},
            "media_sessions": {"token_hash", "captured_privacy_epoch", "max_bytes"},
            "media_requests": {"request_key", "fingerprint", "usage_request_id", "event_id"},
            "content_items": {"owner_event_id", "read_request_id", "usage_request_id", "deadline"},
        }
        columns_present = all(
            columns <= {row["name"] for row in self.rows(f"PRAGMA table_info({table})")}
            for table, columns in required.items()
        )
        content = self.one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='content_items'"
        )
        return columns_present and bool(
            content and "slot<64" in "".join(content["sql"].split()).lower()
        )

    def insert(self, table, **values):
        # Identifiers are application constants, never API input.
        names = ",".join(values)
        marks = ",".join("?" for _ in values)
        self.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(values.values()))

    def audit(self, action, entity_id, trace_id, now, **safe_detail):
        self.insert(
            "audit_log",
            action=action,
            actor_id="system",
            entity_type="core",
            entity_id=entity_id,
            trace_id=trace_id,
            safe_detail_json=encode(safe_detail),
            created_at=now,
        )

    def close(self):
        self.db.close()
