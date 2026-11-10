"""Core state over owned native PostgreSQL sessions; no runtime SQLite backend."""

import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql

from companion.contracts import CoreError
from companion.postgres import BUDGET_LEDGER, ERASURE_LEDGER, TRANSACTION_WRITE_COUNT, Database
from companion.postgres import migrate as migrate


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

def _lock_report(site, started, locked, finished, outcome):
    """Content-free timing of one write transaction, reported only when it is slow."""
    from companion.telemetry import LOCK_REPORT_MS, emit_lock

    wait_ms = (locked - started) * 1000
    held_ms = 0 if finished is None else (finished - locked) * 1000
    if (wait_ms + held_ms < LOCK_REPORT_MS
        and os.environ.get("COMPANION_TELEMETRY_ALL_LOCKS") != "1"):
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
        pass

class Store:
    def __init__(self, database: Database, state_directory):
        if not isinstance(database, Database):
            raise TypeError("Core Store requires a native PostgreSQL Database")
        self.database = database
        self.state_directory = Path(state_directory)
        self._context = database.session()
        self._session = self._context.__enter__()
        self._budget_stops = None
        self._readonly = self._control_write = False
        self._scope = 0
        self._closed = False

    @property
    def in_transaction(self):
        return self._session.in_transaction

    @property
    def backend_pid(self):
        return self._session.backend_pid

    @property
    def in_write_transaction(self):
        return self._budget_stops is not None and self.in_transaction

    @property
    def in_read_snapshot(self):
        return self._readonly and self.in_transaction

    @property
    def control_write(self):
        return self._control_write and self.in_write_transaction

    @property
    def frozen_scope(self):
        # READ COMMITTED writes see other commits between statements. Only the
        # repeatable read-only scope may reuse an unchanged snapshot's summary.
        return self._scope if self.in_read_snapshot else None

    @property
    def validation_scope(self):
        """Identity of a caller-owned transaction; write scopes also need change_token."""
        return self._scope if self.in_read_snapshot or self.in_write_transaction else None

    def change_token(self):
        """Native snapshot plus actual transaction writes, including triggers/CTEs."""
        # A repeatable read-only transaction cannot write business tables. Backend
        # statistics for prior writes can still differ between identical snapshots.
        writes = "'0'::text" if self.in_read_snapshot else TRANSACTION_WRITE_COUNT
        return self.one(
            "SELECT pg_current_snapshot()::text AS snapshot, "
            "current_schema() AS schema, pg_postmaster_start_time()::text AS instance, "
            + writes + " AS writes"
        )

    def execute(self, statement, parameters=()):
        return self._session.execute(statement, parameters)

    def one(self, statement, parameters=()):
        return self._session.one(statement, parameters)

    def rows(self, statement, parameters=()):
        return self._session.rows(statement, parameters)

    def has_table(self, name):
        return bool(self.one(
            "SELECT 1 FROM pg_catalog.pg_class WHERE "
            "relnamespace=current_schema()::regnamespace AND relname=%s "
            "AND relkind IN ('r','p')", (name,),
        ))

    def bounded_read(self, statement, parameters, *, limit, timeout_ms):
        if self.in_transaction and not self.in_read_snapshot:
            raise RuntimeError("BOUNDED_READ_REQUIRES_READ_ONLY_SNAPSHOT")
        return self._session.bounded_read(statement, parameters, limit=limit, timeout_ms=timeout_ms)

    def bootstrap(self, owner_qq_id, now):
        """Seed missing identity once; imports retain their original node incarnation."""
        with self._session.transaction():
            self.execute("SET LOCAL lock_timeout='5s'")
            self.execute("INSERT INTO runtime_meta (key,value) VALUES ('privacy_epoch','0') "
                         "ON CONFLICT (key) DO NOTHING")
            self._control_lock(True)
            for key, value in (("voice_incarnation", uuid4().hex),
                               ("owner_authority_restore_generation", "initial"),
                               ("owner_authority_mode", "OFF")):
                self.execute("INSERT INTO runtime_meta (key,value) VALUES (%s,%s) "
                             "ON CONFLICT (key) DO NOTHING", (key, value))
            previous = self.one("SELECT value FROM runtime_meta WHERE key='owner_qq_id'")
            if previous and previous["value"] != owner_qq_id:
                raise CoreError("OWNER_IDENTITY_MISMATCH", 503)
            self.execute("INSERT INTO runtime_meta (key,value) VALUES ('owner_qq_id',%s) "
                         "ON CONFLICT (key) DO NOTHING", (owner_qq_id,))
            for actor, kind in (("owner", "owner"), ("companion", "companion"),
                                ("system", "system")):
                self.execute("INSERT INTO principals (actor_id,kind,created_at) VALUES (%s,%s,%s) "
                             "ON CONFLICT (actor_id) DO NOTHING", (actor, kind, now))

    def ledger_lock(self, domain, *, shared=False):
        self.ledger_locks(((domain, shared),))

    def ledger_locks(self, domains):
        domains = tuple(domains)
        if self.in_read_snapshot and any(not shared for _, shared in domains):
            raise RuntimeError("READ_SNAPSHOT_WRITE_REQUIRED")
        self._session.ledger_locks(domains)

    @contextmanager
    def read_snapshot(self):
        """Own one repeatable, read-only scope without a write retry."""
        with self._session.transaction(readonly=True):
            self._readonly = True
            self._scope += 1
            try:
                yield self
            finally:
                self._readonly = False

    def read_or_write(self, operation):
        if self.in_transaction:
            return operation()
        caller = sys._getframe(1)
        try:
            with self.read_snapshot():
                return operation()
        except Exception:
            pass  # The authoritative write attempt below decides the result.
        with self.transaction(site=(caller.f_code, caller.f_lineno)):
            return operation()

    def _control_lock(self, exclusive):
        row = self.one(
            "SELECT value FROM runtime_meta WHERE key='privacy_epoch' "
            + ("FOR UPDATE" if exclusive else "FOR SHARE")
        )
        if row is None:
            raise RuntimeError("POSTGRES_RUNTIME_BOOTSTRAP_REQUIRED")

    @contextmanager
    def transaction(
        self, *, site=None, delivery_preflight=False, control_write=False, rollback=False,
    ):
        if self.in_transaction:
            raise RuntimeError("POSTGRES_TRANSACTION_ALREADY_ACTIVE")
        if site is None:
            caller = sys._getframe(2)
            site = (caller.f_code, caller.f_lineno)
        started = locked = time.perf_counter()
        entered = False
        outcome = "ROLLBACK"
        self._budget_stops = set()
        try:
            with self._session.transaction(rollback=rollback):
                self.execute("SET LOCAL lock_timeout='5s'")
                self._control_lock(control_write)
                if delivery_preflight:
                    # These checks already need both native ledger locks. Acquire
                    # them before entering business code so contention retains the
                    # existing no-business-started BUSY receipt and original lease.
                    self._session.ledger_locks(((BUDGET_LEDGER, False), (ERASURE_LEDGER, True)))
                locked = time.perf_counter()
                self._control_write = control_write
                self._scope += 1
                entered = True
                yield self
                # Native NOTIFY commits or rolls back with the business change.
                # Hints carry no content, authority or delivery permission.
                self._session.notify()
            outcome = "ROLLBACK" if rollback else "COMMIT"
        except BaseException as error:
            pending = self._budget_stops
            if pending:
                # Only fsynced stop witnesses survive a business rollback.
                with self._session.transaction():
                    self._control_lock(False)
                    self._session.ledger_lock(BUDGET_LEDGER)
                    for value in pending:
                        self.execute("INSERT INTO runtime_meta (key,value) VALUES (%s,%s) "
                                     "ON CONFLICT (key) DO NOTHING", value)
            if (delivery_preflight and not entered
                and isinstance(error, psycopg.errors.LockNotAvailable)):
                from companion_delivery_protocol.preflight import BUSY

                raise CoreError(BUSY, 503) from error
            raise
        finally:
            self._budget_stops = None
            self._control_write = False
            finished = time.perf_counter()
            _lock_report(site, started, locked if entered else finished,
                         finished if entered else None, outcome)

    def retain_budget_stop(self, journal_id, head_sha256):
        value = ("model_budget_stop:" + head_sha256, journal_id)
        if self._budget_stops is not None:
            self._budget_stops.add(value)
        self.execute("INSERT INTO runtime_meta (key,value) VALUES (%s,%s) "
                     "ON CONFLICT (key) DO NOTHING", value)

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
        present = {}
        for row in self.rows(
            "SELECT table_name,column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema()"
        ):
            present.setdefault(row["table_name"], set()).add(row["column_name"])
        if not all(columns <= present.get(table, set()) for table, columns in required.items()):
            return False
        constraints = self.rows(
            "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
            "WHERE connamespace=current_schema()::regnamespace AND contype='c' "
            "AND conrelid='content_items'::regclass"
        )
        if not any("slot<64" in "".join(row["definition"].split()).replace('"', '')
                   for row in constraints):
            return False
        # These are required runtime shapes, including for an unknown future head.
        # The native baseline already supplies all four; inspection never migrates it.
        return bool(self.one(
            "SELECT 1 WHERE EXISTS (SELECT 1 FROM pg_index i JOIN pg_class x "
            "ON x.oid=i.indexrelid JOIN pg_class t ON t.oid=i.indrelid "
            "WHERE t.relnamespace=current_schema()::regnamespace "
            "AND t.relname='life_topics' AND x.relname='idx_life_active_conversation' "
            "AND NOT i.indisunique) AND EXISTS (SELECT 1 FROM pg_index i "
            "JOIN pg_class x ON x.oid=i.indexrelid JOIN pg_class t ON t.oid=i.indrelid "
            "WHERE t.relnamespace=current_schema()::regnamespace AND t.relname='memories' "
            "AND x.relname='idx_memories_relationship_standing') "
            "AND EXISTS (SELECT 1 FROM pg_index i JOIN pg_class x ON x.oid=i.indexrelid "
            "JOIN pg_class t ON t.oid=i.indrelid "
            "WHERE t.relnamespace=current_schema()::regnamespace AND t.relname='runtime_meta' "
            "AND x.relname='idx_memory_image_review_target') "
            "AND EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='source_grant_revisions' "
            "AND column_name='expires_at' AND is_nullable='YES')"
        ))

    def insert(self, table, **values):
        self.execute(sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, values)),
            sql.SQL(",").join(sql.Placeholder() for _ in values)), tuple(values.values()))

    def audit(self, action, entity_id, trace_id, now, **safe_detail):
        self.insert("audit_log", action=action, actor_id="system", entity_type="core",
                    entity_id=entity_id, trace_id=trace_id, safe_detail_json=encode(safe_detail),
                    created_at=now)

    def close(self):
        if not self._closed:
            if self.in_read_snapshot or self._budget_stops is not None:
                raise RuntimeError("POSTGRES_STORE_SCOPE_ACTIVE")
            self._closed = True
            self._context.__exit__(None, None, None)
