"""One-shot owner memory controls, separately admitted after all chat is stopped.

No model, delivery, extraction or second memory database is involved. The ordinary
API defaults to no grant. Actual admission requires business R5 acceptance and a
new exact approval; synthetic tests cannot supply that evidence.
"""

import hashlib
import hmac
import json
import re
import stat
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import AwareDatetime, Field, model_validator

from companion import live_text, paid_budget
from companion.commands import command_parts
from companion.contracts import Candidate, CoreError, InboundEvent, StrictModel
from companion.memory import Memory
from companion.privacy import epoch, redact, sensitive
from companion.qq_rich import check_http_json, input_digest
from companion.store import encode, uid
from companion.worker_lock import single_worker

SIGNATURE_DOMAIN = b"companion-core/memory-control-capture/v1\x00"
RECEIPT = "memory_control_receipt:"
RESTORED = "memory_control_restore_generation"
SHA = r"^[a-f0-9]{64}$"
OPERATIONS = {"记住这个": "remember", "确认这一条": "confirm",
              "纠正这一条": "correct", "忘记这一条": "forget"}


class MemoryControlGrant(StrictModel):
    execution: Literal["local_synthetic", "approved_stopped_r5"]
    b1_acceptance_sha256: str | None = Field(default=None, pattern=SHA)
    owner_approval_sha256: str = Field(pattern=SHA)
    stopped_services_sha256: str = Field(pattern=SHA)
    grant_id: str = Field(pattern=r"^mc_[a-f0-9]{32}$")
    operation: Literal["remember", "confirm", "correct", "forget"]
    permission: Literal["memory.write", "memory.correct", "memory.forget"]
    max_commands: Literal[1]
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    privacy_epoch: int = Field(ge=0, strict=True)
    restore_generation: str | None = Field(default=None, pattern=r"^restore_[a-f0-9]{32}$")
    ledger_sha256: str = Field(pattern=SHA)
    stopped_state_sha256: str = Field(pattern=SHA)
    predecessor_window_sha256: str | None = Field(default=None, pattern=SHA)
    bridge_journal_head_sha256: str | None = Field(default=None, pattern=SHA)
    identity_sha256: str = Field(pattern=SHA)
    core_config_sha256: str = Field(pattern=SHA)
    core_code_sha256: str = Field(pattern=SHA)
    bridge_config_sha256: str = Field(pattern=SHA)
    bridge_code_sha256: str = Field(pattern=SHA)
    capture_public_key: str = Field(pattern=SHA)
    protocol_epoch: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    command_sha256: str = Field(pattern=SHA)
    target_memory_id: str | None = Field(default=None, pattern=r"^mem_[a-f0-9]{32}$")
    target_sha256: str | None = Field(default=None, pattern=SHA)
    subject: Literal["user", "project"] = "user"
    fact_key: str = Field(min_length=1, max_length=80)
    sensitive_confirmed: bool = False
    sensitive_confirmation_sha256: str | None = Field(default=None, pattern=SHA)

    @model_validator(mode="after")
    def scope(self):
        required = {"remember": "memory.write", "confirm": "memory.write",
                    "correct": "memory.correct", "forget": "memory.forget"}[self.operation]
        if self.permission != required or not 0 < (
            self.ends_at - self.starts_at
        ).total_seconds() <= 900:
            raise ValueError("INVALID_MEMORY_CONTROL_SCOPE")
        if self.execution == "approved_stopped_r5" and not (
            self.b1_acceptance_sha256 and self.predecessor_window_sha256
            and self.bridge_journal_head_sha256
        ):
            raise ValueError("BUSINESS_R5_ACCEPTANCE_AND_STOP_REQUIRED")
        if self.sensitive_confirmed and not self.sensitive_confirmation_sha256:
            raise ValueError("SENSITIVE_CONFIRMATION_EVIDENCE_REQUIRED")
        if (self.operation == "remember") != (
            self.target_memory_id is None and self.target_sha256 is None
        ) or (self.operation != "remember" and not (
            self.target_memory_id and self.target_sha256
        )):
            raise ValueError("EXACT_MEMORY_TARGET_REQUIRED")
        return self


class MemoryControlInput(StrictModel):
    event: InboundEvent
    capture: live_text.CaptureReceipt


def load_grant(path, approved_sha256):
    target = Path(path)
    info = target.lstat()
    if (not stat.S_ISREG(info.st_mode) or target.is_symlink() or info.st_size > 16384
        or (__import__("os").name != "nt" and info.st_mode & 0o077)):
        raise CoreError("PRIVATE_MEMORY_CONTROL_APPROVAL_REQUIRED", 403)
    raw = target.read_bytes()
    check_http_json(raw)
    value = json.loads(raw)
    grant = MemoryControlGrant.model_validate(value)
    if value != grant.model_dump(mode="json"):
        raise CoreError("MEMORY_CONTROL_CANONICAL_APPROVAL_REQUIRED", 400)
    if not hmac.compare_digest(paid_budget.sha(grant.model_dump(mode="json")), approved_sha256):
        raise CoreError("MEMORY_CONTROL_APPROVAL_MISMATCH", 403)
    return grant


@contextmanager
def stopped_workers(state_directory):
    """Hold the existing OS locks for the entire separately launched control API lifetime."""
    with ExitStack() as stack:
        for kind in ("worker", "diary-worker", "life-worker", "executive-worker", "content-worker"):
            stack.enter_context(single_worker(state_directory, kind))
        yield


def target_digest(store, memory_id):
    """Bind the reviewed target, correction family and shared evidence deletion closure."""
    memories = store.rows('SELECT memories.memory_id AS memory_id, memories.subject AS subject, '
        'memories.kind AS kind, memories.content AS content, memories.status AS status, '
        'memories.origin AS origin, memories.confidence AS confidence, memories.importance AS '
        'importance, memories.privacy_scope AS privacy_scope, memories.supersedes_memory_id AS '
        'supersedes_memory_id, memories.created_at AS created_at, memories.updated_at AS '
        'updated_at, memories.valid_from AS valid_from, memories.valid_to AS valid_to, '
        'memories.last_reviewed_at AS last_reviewed_at, memories.fact_key AS fact_key, '
        'memories.needs_review AS needs_review FROM memories AS memories ORDER BY '
        'memories.memory_id NULLS FIRST')
    links = store.rows('SELECT memory_evidence.memory_id AS memory_id, memory_evidence.event_id AS '
        'event_id, memory_evidence.polarity AS polarity, memory_evidence.created_at AS created_at '
        'FROM memory_evidence AS memory_evidence ORDER BY memory_evidence.memory_id NULLS FIRST, '
        'memory_evidence.event_id NULLS FIRST')
    ids, events = {memory_id}, set()
    while True:
        previous = len(ids), len(events)
        for row in memories:
            if row["memory_id"] in ids or row["supersedes_memory_id"] in ids:
                ids.add(row["memory_id"])
                if row["supersedes_memory_id"]:
                    ids.add(row["supersedes_memory_id"])
        for row in links:
            if row["memory_id"] in ids:
                events.add(row["event_id"])
            if row["event_id"] in events:
                ids.add(row["memory_id"])
        if previous == (len(ids), len(events)):
            break
    selected = [row for row in memories if row["memory_id"] in ids]
    if not any(row["memory_id"] == memory_id for row in selected):
        raise CoreError("MEMORY_NOT_FOUND", 404)
    return paid_budget.sha({"memories": selected,
        "evidence": [row for row in links if row["memory_id"] in ids],
        "events": store.rows('SELECT events.event_id AS event_id, events.schema_version AS '
            'schema_version, events.source AS source, events.source_event_id AS source_event_id, '
            'events.conversation_id AS conversation_id, events.actor_id AS actor_id, '
            'events.event_type AS event_type, events.occurred_at AS occurred_at, '
            'events.ingested_at AS ingested_at, events.privacy_scope AS privacy_scope, '
            'events.body_json AS body_json, events.trace_id AS trace_id, events.idempotency_key AS '
            'idempotency_key, events.fingerprint AS fingerprint, events.redacted AS redacted, '
            'events.memory_processed AS memory_processed, events.reply_revision AS reply_revision '
            'FROM events AS events WHERE events.event_id IN (SELECT parsed.value COLLATE "C" AS '
            '_col_0 FROM JSON_ARRAY_ELEMENTS_TEXT(CAST(%s AS JSON)) AS parsed(value)) ORDER BY '
            'events.event_id NULLS FIRST',
                             (encode(sorted(events)),))})


def stopped_state(store):
    """Read the existing STOP and budget custody; never create or reset a journal."""
    live_text._idle(store)
    binding = live_text._meta(store, live_text.BINDING)
    custody = None
    if binding:
        history = live_text._meta(store, live_text.HISTORY + binding["plan_sha256"])
        if not history or history["state"] != "STOPPED" or not live_text.stopped(store):
            raise CoreError("MEMORY_CONTROL_STOP_REQUIRED", 403)
        custody = paid_budget.journal(store, SimpleNamespace(
            journal_path=binding["budget_journal_path"]), allow_settlement=True)
        if not custody["lifecycle"] or custody["lifecycle"]["state"] != "STOPPED":
            raise CoreError("MEMORY_CONTROL_STOP_REQUIRED", 403)
    elif store.one('SELECT 1 FROM model_admissions LIMIT 1') or store.one(
        "SELECT 1 FROM runtime_meta WHERE key = 'model_budget_live_lifecycle'"
    ):
        raise CoreError("MEMORY_CONTROL_STOP_REQUIRED", 403)
    return paid_budget.sha({"binding": binding, "custody": custody,
        "windows": store.rows('SELECT runtime_meta.key AS key, runtime_meta.value AS value FROM '
            "runtime_meta AS runtime_meta WHERE runtime_meta.key LIKE 'live_text_%%' OR "
            "runtime_meta.key LIKE 'model_budget_%%' ORDER BY runtime_meta.key NULLS FIRST"),
        "usage": store.rows('SELECT model_usage.usage_id AS usage_id, model_usage.request_id AS '
            'request_id, model_usage.job_id AS job_id, model_usage.scenario AS scenario, '
            'model_usage.provider AS provider, model_usage.model AS model, '
            'model_usage.input_tokens AS input_tokens, model_usage.cached_input_tokens AS '
            'cached_input_tokens, model_usage.output_tokens AS output_tokens, '
            'model_usage.billed_micro_usd AS billed_micro_usd, model_usage.is_estimate AS '
            'is_estimate, model_usage.occurred_at AS occurred_at, model_usage.state AS state, '
            'model_usage.pricing_known AS pricing_known FROM model_usage AS model_usage ORDER BY '
            'model_usage.request_id NULLS FIRST'),
        "admissions": store.rows('SELECT model_admissions.request_id AS request_id, '
            'model_admissions.logical_request_id AS logical_request_id, '
            'model_admissions.attempt_id AS attempt_id, model_admissions.attempt_number AS '
            'attempt_number, model_admissions.window_id AS window_id, '
            'model_admissions.contract_sha256 AS contract_sha256, model_admissions.payload_sha256 '
            'AS payload_sha256, model_admissions.snapshot_json AS snapshot_json, '
            'model_admissions.reserved_micro_usd AS reserved_micro_usd, '
            'model_admissions.input_bound AS input_bound, model_admissions.output_bound AS '
            'output_bound, model_admissions.lease_token AS lease_token, '
            'model_admissions.lease_until AS lease_until, model_admissions.io_started_at AS '
            'io_started_at, model_admissions.journal_path AS journal_path, '
            'model_admissions.journal_id AS journal_id, model_admissions.measurement_json AS '
            'measurement_json, model_admissions.authority_json AS authority_json FROM '
            'model_admissions AS model_admissions ORDER BY model_admissions.request_id NULLS '
            'FIRST')})


class StoppedMemoryControl:
    def __init__(self, store, settings, ledger, clock, grant, approved_sha256):
        self.store, self.settings, self.ledger, self.clock = store, settings, ledger, clock
        self.grant = MemoryControlGrant.model_validate(grant)
        self.sha256 = paid_budget.sha(self.grant.model_dump(mode="json"))
        if not hmac.compare_digest(self.sha256, approved_sha256):
            raise CoreError("MEMORY_CONTROL_APPROVAL_MISMATCH", 403)
        # Isolated service object only: do not turn on extraction in Core settings.
        self.memory = Memory(store, settings.model_copy(update={"memory_enabled": True}), ledger)

    def check(self):
        cfg, g, s = self.settings, self.grant, self.store
        now = datetime.fromisoformat(self.clock())
        if g.execution == "local_synthetic" and (cfg.mode != "demo" or cfg.paid_model is not None):
            raise CoreError("MEMORY_CONTROL_OFFLINE_ONLY", 403)
        binding = live_text._meta(s, live_text.BINDING)
        if g.execution == "approved_stopped_r5" and (
            not binding or binding["plan_sha256"] != g.predecessor_window_sha256
        ):
            raise CoreError("MEMORY_CONTROL_STOP_REQUIRED", 403)
        if any(getattr(cfg, flag) for flag in (
            "memory_enabled", "diary_enabled", "affect_enabled", "consolidation_enabled",
            "reflection_enabled", "goals_enabled", "wake_enabled", "exploration_enabled",
            "social_enabled", "proactive_enabled", "content_enabled", "observation_enabled",
        )):
            raise CoreError("MEMORY_CONTROL_OTHER_WRITERS_ENABLED", 403)
        restored = s.one('SELECT value FROM runtime_meta WHERE key = %s', (RESTORED,))
        if (restored["value"] if restored else None) != g.restore_generation:
            raise CoreError("MEMORY_CONTROL_RESTORED_REAPPROVAL_REQUIRED", 403)
        if not g.starts_at <= now < g.ends_at:
            raise CoreError("MEMORY_CONTROL_EXPIRED", 403)
        if (g.identity_sha256 != paid_budget.sha([cfg.owner_qq_id, cfg.bot_qq_id])
            or s.one("SELECT value FROM runtime_meta WHERE key = 'owner_qq_id'")
            != {"value": cfg.owner_qq_id}
            or g.core_config_sha256 != live_text.config_sha256(cfg)
            or g.core_code_sha256 != live_text.code_sha256()):
            raise CoreError("MEMORY_CONTROL_BINDING_CHANGED", 403)
        if stopped_state(s) != g.stopped_state_sha256:
            raise CoreError("MEMORY_CONTROL_CUSTODY_CHANGED", 409)
        live_text._ledger_custody(s, cfg)
        return now

    def verify_source(self, body, now):
        g, event, capture = self.grant, body.event, body.capture
        text = event.payload.text
        origin = f"onebot-direct:FriendMessage:{self.settings.owner_qq_id}"
        prefix = f"{self.settings.bot_qq_id}:{origin}:epoch:{g.protocol_epoch}:"
        if (event.actor.platform_user_id != self.settings.owner_qq_id
            or event.conversation.platform_session_key != origin
            or not event.source_event_id.startswith(prefix)
            or not re.fullmatch(r"-?[1-9][0-9]{0,19}", event.source_event_id.removeprefix(prefix))
            or event.event_type != "message.received"
            or event.event_id != "evt_" + hashlib.sha256(event.source_event_id.encode()).hexdigest()
            or event.trace_id != "trace_" + event.event_id.removeprefix("evt_")
            or event.payload.media or event.payload.shared_content or event.payload.rich
            or not text.startswith("/") or "[CQ:" in text
            or redact(text, self.settings) != text
            or capture.window_sha256 != self.sha256
            or capture.event_id != event.event_id
            or capture.source_event_id != event.source_event_id
            or capture.input_sha256 != input_digest(event.payload.model_dump())
            or capture.input_sha256 != g.command_sha256
            or capture.identity_sha256 != g.identity_sha256
            or capture.config_sha256 != g.bridge_config_sha256
            or capture.protocol_epoch != g.protocol_epoch
            or capture.occurred_at != event.occurred_at
            or not g.starts_at <= capture.occurred_at <= capture.received_at <= now < g.ends_at
            or (now - capture.received_at).total_seconds() > 30
            or (capture.received_at - capture.occurred_at).total_seconds() > 30):
            raise CoreError("MEMORY_CONTROL_SOURCE_INVALID", 403)
        try:
            signed = capture.model_dump(mode="json", exclude={"signature"})
            for field in ("received_at", "occurred_at"):
                signed[field] = getattr(capture, field).isoformat()
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(g.capture_public_key)).verify(
                bytes.fromhex(capture.signature or ""), SIGNATURE_DOMAIN + encode(signed).encode())
        except (ValueError, InvalidSignature):
            raise CoreError("MEMORY_CONTROL_CAPTURE_INVALID", 403) from None
        command, arguments = command_parts(text)
        if OPERATIONS.get(command) != g.operation:
            raise CoreError("MEMORY_CONTROL_OPERATION_DENIED", 403)
        if g.operation == "remember":
            content = arguments
        else:
            parts = arguments.split(maxsplit=1)
            if not parts or parts[0] != g.target_memory_id or (
                g.operation == "correct" and len(parts) != 2
            ) or (g.operation != "correct" and len(parts) != 1):
                raise CoreError("MEMORY_CONTROL_TARGET_MISMATCH", 403)
            content = parts[1] if len(parts) == 2 else None
        if content is not None and not 2 <= len(content) <= 500:
            raise CoreError("MEMORY_CONTROL_FACT_REQUIRED", 400)
        if sensitive(content or "") and not g.sensitive_confirmed:
            raise CoreError("MEMORY_CONTROL_SENSITIVE_CONFIRMATION_REQUIRED", 403)
        return content

    def apply(self, body):
        body = MemoryControlInput.model_validate(body)
        s, g, event = self.store, self.grant, body.event
        with s.transaction(control_write=True):
            now = self.check()
            content = self.verify_source(body, now)
            key = RECEIPT + g.grant_id
            prior = live_text._meta(s, key)
            # Reconnection can produce a fresh signed capture of the same platform message.
            # Revalidate that proof above, but deduplicate by its immutable normalized source.
            fingerprint = paid_budget.sha(event.model_dump(mode="json"))
            if prior:
                if prior["grant_sha256"] != self.sha256 or prior["source_sha256"] != fingerprint:
                    raise CoreError("MEMORY_CONTROL_ALREADY_CONSUMED", 409)
                # A later correction/forget cannot make a stale success look current.
                if prior["privacy_epoch"] != epoch(s):
                    raise CoreError("MEMORY_CONTROL_RESULT_STALE", 409)
                return {**prior, "duplicate": True}
            if epoch(s) != g.privacy_epoch:
                raise CoreError("PRIVACY_CHANGED", 409)
            live_text._ledger_custody(s, self.settings, g.ledger_sha256)
            if g.target_memory_id and target_digest(s, g.target_memory_id) != g.target_sha256:
                raise CoreError("MEMORY_CONTROL_TARGET_CHANGED", 409)
            if g.operation == "remember" and s.one(
                "SELECT 1 FROM memories WHERE subject = %s AND status IN ('HYPOTHESIS', "
                "'CONFIRMED') AND (content = %s OR fact_key = %s)", (g.subject, content, g.fact_key)
            ):
                raise CoreError("MEMORY_CONTROL_EXPLICIT_EXISTING_TARGET_REQUIRED", 409)
            if g.operation in {"confirm", "correct"} and not s.one(
                'SELECT 1 FROM memory_evidence AS v JOIN events AS e USING (event_id) WHERE '
                "v.memory_id = %s AND e.redacted = 0 AND e.actor_id = 'owner' AND e.privacy_scope "
                "= 'private'", (g.target_memory_id,)
            ):
                raise CoreError("MISSING_MEMORY_EVIDENCE", 409)
            if s.one('SELECT 1 FROM runtime_meta WHERE key = '
                '%s', ("erased_event:" + event.event_id,)):
                raise CoreError("EVENT_ERASED", 410)
            if s.one('SELECT 1 FROM events WHERE event_id = %s OR source_event_id = %s',
                     (event.event_id, event.source_event_id)):
                raise CoreError("MEMORY_CONTROL_SOURCE_REPLAY", 409)
            conversation = s.one('SELECT conversation_id FROM conversations WHERE platform = %s '
                'AND platform_session_key = %s', (event.source,
                                 event.conversation.platform_session_key))
            conversation_id = conversation["conversation_id"] if conversation else uid("conv")
            if not conversation:
                s.insert("conversations", conversation_id=conversation_id, platform=event.source,
                         platform_session_key=event.conversation.platform_session_key,
                         scope="private", owner_actor_id="owner", created_at=self.clock())
            s.insert("events", event_id=event.event_id, schema_version=1, source=event.source,
                     source_event_id=event.source_event_id, conversation_id=conversation_id,
                     actor_id="owner", event_type=event.event_type,
                     occurred_at=event.occurred_at.isoformat(), ingested_at=self.clock(),
                     privacy_scope="private", body_json=encode({"text": event.payload.text,
                         "media": [], "control_command": True}), trace_id=event.trace_id,
                     idempotency_key="memory-control:" + event.event_id,
                     fingerprint=fingerprint, memory_processed=1)
            if g.operation == "remember":
                memory_id = self.memory.add(Candidate(subject=g.subject, kind="fact",
                    fact_key=g.fact_key, content=content, evidence_event_id=event.event_id,
                    evidence_quote=content), self.clock(), confirmed=True)
                if memory_id is None:
                    raise CoreError("MEMORY_CONTROL_FACT_REJECTED", 409)
                result = {"memory_id": memory_id, "state": "CONFIRMED"}
            else:
                result = self.memory.decide(g.target_memory_id, g.operation, self.clock(), content,
                                            evidence_event_id=event.event_id)
            # Ledger-first deletion must be durably applied before publishing a receipt.
            live_text._ledger_custody(s, self.settings)
            result = {**result, "grant_sha256": self.sha256, "source_sha256": fingerprint,
                      "privacy_epoch": epoch(s), "duplicate": False}
            live_text._put(s, key, result)
            s.audit("memory.control", g.grant_id, event.trace_id, self.clock(),
                    permission=g.permission, grant_sha256=self.sha256,
                    capture_sha256=paid_budget.sha(body.capture.model_dump(mode="json")),
                    source_event_id=event.event_id, privacy_epoch=epoch(s))
            return result


