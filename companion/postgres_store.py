"""Core transaction scopes over native PostgreSQL sessions.

Staged for runtime adoption; no backend selector or SQLite SQL translation lives here.
The Runtime owns the Database, and each finite operation owns one Store/session.
"""

import sys
import time
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql

from companion.contracts import CoreError
from companion.postgres import Database
from companion.store import _lock_report, encode

BUDGET_LEDGER = 1
ERASURE_LEDGER = 2


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

    def change_token(self):
        """Native snapshot plus actual transaction writes, including triggers/CTEs."""
        return self.one(
            "SELECT pg_current_snapshot()::text AS snapshot, "
            "COALESCE((SELECT sum(n_tup_ins+n_tup_upd+n_tup_del) "
            "FROM pg_stat_xact_user_tables WHERE schemaname=current_schema()),0)::text AS writes"
        )

    def execute(self, statement, parameters=()):
        return self._session.execute(statement, parameters)

    def one(self, statement, parameters=()):
        return self._session.one(statement, parameters)

    def rows(self, statement, parameters=()):
        return self._session.rows(statement, parameters)

    def ledger_lock(self, domain, *, shared=False):
        if self.in_read_snapshot and not shared:
            raise RuntimeError("READ_SNAPSHOT_WRITE_REQUIRED")
        self._session.ledger_lock(domain, shared=shared)

    def read_or_write(self, operation):
        if self.in_transaction:
            return operation()
        caller = sys._getframe(1)
        try:
            with self._session.transaction(readonly=True):
                self._readonly = True
                self._scope += 1
                try:
                    return operation()
                finally:
                    self._readonly = False
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
    def transaction(self, *, site=None, delivery_preflight=False, control_write=False):
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
            with self._session.transaction():
                self.execute("SET LOCAL lock_timeout='5s'")
                self._control_lock(control_write)
                locked = time.perf_counter()
                self._control_write = control_write
                self._scope += 1
                entered = True
                yield self
            outcome = "COMMIT"
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
        return any("slot<64" in "".join(row["definition"].split()).replace('"', '')
                   for row in constraints)

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
