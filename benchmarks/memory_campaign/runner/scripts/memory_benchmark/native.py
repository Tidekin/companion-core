"""Public-synthetic native Core memory evaluation; never imports grading material.

Historical assistant replies are supplied transcript fixtures, never regenerated.
Each exam uses a fresh native SDK conversation. Native source erasure after each
exam removes its inputs/outputs while retaining every paid-accounting liability.
This sequential isolation differs from upstream's per-question writable copies.
"""

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import owner_authority, owner_input, paid_budget
from companion.contracts import InboundEvent
from companion.core import Core
from companion.memory_work import checkpoint_state
from companion.model import ModelClient
from companion.owner_authority_protocol import (
    AuthorityChange,
    AuthorityPreview,
    OwnerCapture,
    OwnerGrantPolicy,
    OwnerRuntimeConfig,
)
from companion.qq_rich import input_digest
from companion.store import encode, migrate
from companion.transport import enqueue
from companion_delivery_protocol.authority import capture_signing_payload
from scripts.memory_benchmark.runtime import native_settings

settings = native_settings


def execution_audit(core):
    """Export verified native journal closures; never settle or release a liability."""
    from companion import model_carry

    approval = core.settings.paid_model
    view = paid_budget.journal(core.store, approval, allow_settlement=True)
    uncertain = core.store.rows(
        "SELECT request_id,state,billed_micro_usd FROM model_usage "
        "WHERE state IN ('UNKNOWN','RESERVED') ORDER BY request_id"
    )
    closures = view["model_closures"] if model_carry.active(core.settings) else {}
    return {
        "basis": approval.concurrency_basis,
        "journal_sha256": hashlib.sha256(Path(approval.journal_path).read_bytes()).hexdigest(),
        "uncertain": uncertain,
        "closed_unknowns": {
            r["request_id"]: closures[r["request_id"]]
            for r in uncertain
            if r["state"] == "UNKNOWN" and r["request_id"] in closures
        },
        "unclosed_requests": [
            r["request_id"] for r in uncertain if r["request_id"] not in closures
        ],
    }


def unknown_locally_closed(core, usages):
    uncertain = [u for u in usages if u["state"] in {"UNKNOWN", "RESERVED"}]
    if not uncertain or any(u["state"] != "UNKNOWN" for u in uncertain):
        return False
    audit = execution_audit(core)
    return not audit["unclosed_requests"] and all(
        u["request_id"] in audit["closed_unknowns"] for u in uncertain
    )


__all__ = [
    "Clock",
    "Core",
    "Ed25519PrivateKey",
    "Harness",
    "ModelClient",
    "OwnerGrantPolicy",
    "OwnerRuntimeConfig",
    "AuthorityPreview",
    "AuthorityChange",
    "append",
    "digest",
    "encode",
    "migrate",
    "owner_authority",
    "paid_budget",
    "save",
    "settings",
]


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def append(path, value):
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(value, ensure_ascii=False) + "\n")
        out.flush()
        os.fsync(out.fileno())


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Clock:
    def __init__(self, value):
        self.set(value)

    def set(self, value):
        self.now = datetime.fromisoformat(value).astimezone(UTC)

    def __call__(self):
        return self.now.isoformat(timespec="microseconds")


async def run_with_retries(core, label, job_id):
    """Let Core's own RETRY_WAIT backoff run, as production would, for transport errors.

    Every attempt and its paid usage stays recorded; only Core's final answer is kept.
    """
    await core.run_once(label, job_id=job_id)
    for _ in range(3):
        job = core.store.one(
            "SELECT state,last_error_code,next_attempt_at FROM jobs WHERE job_id=?", (job_id,)
        )
        if job["state"] != "RETRY_WAIT" or job["last_error_code"] != "MODEL_RETRYABLE":
            return
        if job["next_attempt_at"]:
            due = datetime.fromisoformat(job["next_attempt_at"]).astimezone(UTC)
            if due >= core.clock.now:
                core.clock.now = due + timedelta(seconds=1)
        await core.run_once(label, job_id=job_id)


class Harness:
    def __init__(self, core, key, authority, directory):
        self.core, self.key, self.authority, self.directory = core, key, authority, directory
        self.phase, self.case, self.imported = "setup", None, 0
        self.turn_ids = set()

    def ingest(self, text, label, payload=None):
        core = self.core
        origin = "onebot-direct:FriendMessage:10001"
        number = (
            int(label.split("-")[-1])
            if label.startswith("turn-")
            else 100000 + int(digest(label)[:10], 16)
        )
        source = "10002:" + origin + ":epoch:public-memory-benchmark-v1:" + str(number)
        event_id = "evt_" + hashlib.sha256(source.encode()).hexdigest()
        at = core.clock()
        record = {
            "authority": self.authority,
            "kind": "authenticated_onebot_ws",
            "source_event_id": source,
            "event_id": event_id,
            "input_sha256": input_digest(payload or {"text": text}),
            "frame_sha256": digest({"synthetic_import": label, "text": text}),
            "evidence_sha256": digest(["PUBLIC_SYNTHETIC", label, at]),
            "identity_sha256": paid_budget.sha(["10001", "10002"]),
            "protocol_epoch": "public-memory-benchmark-v1",
            "occurred_at": at,
            "received_at": at,
        }
        record["signature"] = self.key.sign(capture_signing_payload(record)).hex()
        owner_input.approve(
            core.store, core.settings, core.clock, OwnerCapture.model_validate(record)
        )
        event = InboundEvent.model_validate(
            {
                "schema_version": 1,
                "event_id": event_id,
                "source": "qq.onebot",
                "source_event_id": source,
                "event_type": "message.received",
                "occurred_at": at,
                "conversation": {"platform_session_key": origin, "scope": "private"},
                "actor": {"platform_user_id": "10001", "role": "owner"},
                "payload": payload or {"text": text, "media": []},
                # Telemetry accepts only trace_<hex>; the label stays derivable and stable.
                "trace_id": "trace_" + hashlib.sha256(label.encode()).hexdigest()[:32],
            }
        )
        admitted = core.ingest(event, "public-memory:" + label)
        return admitted, event_id

    def import_turn(self, turn):
        admitted, event_id = self.ingest(turn["用户消息"], f"turn-{turn['轮次']:03}")
        s, at = self.core.store, self.core.clock()
        job = s.one(
            "SELECT j.*,e.conversation_id,e.body_json,e.trace_id FROM jobs j JOIN"
            " events e USING(event_id) WHERE job_id=?",
            (admitted["job_id"],),
        )
        # Fixture import: supplied assistant text is not a generated/provider/QQ receipt.
        with s.transaction():
            message = enqueue(s, event_id, job["conversation_id"], turn["助手回复"], at)
            s.execute(
                "UPDATE outbox SET state='SENT',sent_at=?,updated_at=? WHERE message_id=?",
                (at, at, message),
            )
            s.execute(
                "UPDATE jobs SET state='SUCCEEDED',updated_at=? WHERE job_id=?",
                (at, admitted["job_id"]),
            )
        self.turn_ids.add(event_id)
        self.imported += 1

    async def maintenance(self):
        self.phase, self.case = "maintenance", None
        core = self.core
        before = core.store.one("SELECT COUNT(*) n FROM model_usage")["n"]
        result = await core.extract_once()
        after = core.store.one("SELECT COUNT(*) n FROM model_usage")["n"]
        if after != before:
            checkpoint = checkpoint_state(core.store)
            record = {
                "now": core.clock(),
                "cutoff_turn": self.imported,
                "success": result,
                "new_calls": after - before,
                "checkpoint": checkpoint,
            }
            append(self.directory / "maintenance.jsonl", record)
            print(
                f"maintenance prefix={self.imported} calls={after - before} complete={result}",
                flush=True,
            )
            uncertain = core.store.rows(
                "SELECT request_id,state FROM model_usage WHERE state IN ('UNKNOWN','RESERVED')"
            )
            if uncertain and not unknown_locally_closed(core, uncertain):
                raise RuntimeError("NATIVE_MODEL_USAGE_UNKNOWN")

    def memory_fingerprint(self):
        s = self.core.store
        return digest(
            {
                table: s.rows(f"SELECT * FROM {table} ORDER BY rowid")
                for table in (
                    "memories",
                    "memory_evidence",
                    "episodes",
                    "episode_events",
                    "conversation_summaries",
                )
            }
        )

    async def exam(self, q):
        core, s = self.core, self.core.store
        self.phase, self.case = "exam", q["id"]
        assert self.imported == q["cutoff_turn"]
        # No future sources, and no prior exam can remain available to history/recall.
        active = s.rows(
            "SELECT event_id,source_event_id,occurred_at FROM events "
            "WHERE redacted=0 AND source='qq.onebot'"
        )
        assert all(row["event_id"] in self.turn_ids for row in active)
        assert all(row["occurred_at"] <= core.clock() for row in active)
        before_memory = self.memory_fingerprint()
        corpus_summaries = s.rows("SELECT * FROM conversation_summaries ORDER BY rowid")
        corpus_episodes = s.rows("SELECT * FROM episodes ORDER BY rowid")
        corpus_replies = s.rows(
            "SELECT o.* FROM outbox o JOIN events e ON e.event_id=o.origin_event_"
            "id WHERE e.redacted=0 ORDER BY o.rowid"
        )
        usage_before = {r["request_id"] for r in s.rows("SELECT request_id FROM model_usage")}
        admitted, event_id = self.ingest(q["question"], "exam-" + q["id"].replace("/", "-"))
        s.execute("UPDATE events SET memory_processed=1 WHERE event_id=?", (event_id,))
        job = s.one(
            "SELECT j.*,e.conversation_id,e.body_json,e.trace_id FROM jobs j JOIN"
            " events e USING(event_id) WHERE job_id=?",
            (admitted["job_id"],),
        )
        # Native history nomination, independently saved before any model answer.
        history = owner_input.history_sources(s, core.settings, core.clock, job)
        assert len(history) <= 6
        assert all(h["event_id"] in self.turn_ids for h in history)
        started = datetime.now(UTC)
        core.clock.now += timedelta(seconds=getattr(self, "advance_exam_seconds", 10))
        await run_with_retries(core, "public-memory-exam", admitted["job_id"])
        job = s.one(
            "SELECT j.*,e.conversation_id,e.body_json,e.trace_id FROM jobs j JOIN"
            " events e USING(event_id) WHERE job_id=?",
            (admitted["job_id"],),
        )
        parts = s.rows(
            "SELECT content_json,state FROM outbox WHERE origin_event_id=? ORDER BY rowid",
            (event_id,),
        )
        answer = "\n".join(json.loads(p["content_json"]).get("text", "") for p in parts)
        usages = [
            r for r in s.rows("SELECT * FROM model_usage") if r["request_id"] not in usage_before
        ]
        record = {
            **q,
            "status": job["state"],
            "error": job["last_error_code"],
            "answer": answer,
            "answer_stage": "native Outbox, no QQ transport",
            "actual_started_at": started.isoformat(),
            "actual_finished_at": datetime.now(UTC).isoformat(),
            "history_source_ids": [h["event_id"] for h in history],
            "short_term_turns": len(history),
            "memory_state_sha256": before_memory,
            "usage": usages,
            "event_id": event_id,
        }
        skipped = job["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"} and (
            job["last_error_code"] in {"MODEL_RETRYABLE", "MODEL_OUTCOME_UNKNOWN"}
            and unknown_locally_closed(core, usages)
        )
        if skipped:
            append(
                self.directory / "infrastructure-errors.jsonl", {**record, "job_id": job["job_id"]}
            )
        elif job["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            # Preserve a resumable native pending job. A provider error or paid
            # uncertainty must never erase it and advance to later questions.
            append(self.directory / "pending-exams.jsonl", record)
            save(
                self.directory / "pending-exam-snapshot.json",
                {
                    "question": q,
                    "event_id": event_id,
                    "job_id": job["job_id"],
                    "memory_state_sha256": before_memory,
                    "corpus_summaries": corpus_summaries,
                    "corpus_episodes": corpus_episodes,
                    "corpus_replies": corpus_replies,
                    "usage_before": sorted(usage_before),
                },
            )
            raise RuntimeError(
                "NATIVE_EXAM_NONTERMINAL:" + job["state"] + ":" + str(job["last_error_code"])
            )
        with s.transaction():
            core.ledger.expire_events(s, [event_id], core.clock())
            # Restore only the pre-exam fictional fixture summary snapshot. Native
            # erasure conservatively clears conversation-wide summaries. Money,
            # admission, erasure and audit rows always retain the actual exam.
            for reply in corpus_replies:
                assert reply["origin_event_id"] in self.turn_ids
                assert (
                    s.one(
                        "SELECT redacted FROM events WHERE event_id=?", (reply["origin_event_id"],)
                    )["redacted"]
                    == 0
                )
                s.execute(
                    "UPDATE outbox SET content_json=?,content_sha256=? WHERE message_id=?",
                    (reply["content_json"], reply["content_sha256"], reply["message_id"]),
                )
            for episode in corpus_episodes:
                assert not s.one(
                    "SELECT 1 FROM episode_events x JOIN events e USING(event_id) WHERE x"
                    ".episode_id=? AND e.redacted=1",
                    (episode["episode_id"],),
                )
                s.execute(
                    "UPDATE episodes SET title=?,summary=?,status=? WHERE episode_id=?",
                    (
                        episode["title"],
                        episode["summary"],
                        episode["status"],
                        episode["episode_id"],
                    ),
                )
            for summary in corpus_summaries:
                assert all(
                    s.one("SELECT redacted FROM events WHERE event_id=?", (summary[k],))["redacted"]
                    == 0
                    for k in ("first_event_id", "last_event_id")
                )
                if not s.one(
                    "SELECT 1 FROM conversation_summaries WHERE summary_id=?",
                    (summary["summary_id"],),
                ):
                    s.insert("conversation_summaries", **summary)
        assert self.memory_fingerprint() == before_memory, "EXAM_CHANGED_PERSISTENT_MEMORY"
        assert s.one("SELECT redacted FROM events WHERE event_id=?", (event_id,))["redacted"] == 1
        if skipped:
            terminal = s.one("SELECT state FROM jobs WHERE job_id=?", (job["job_id"],))["state"]
            assert terminal == "CANCELLED", "NATIVE_EXAM_NOT_CANCELLED_BY_ERASURE"
            record.update(
                status=terminal, native_state_before_skip=job["state"], infrastructure_error=True
            )
        append(self.directory / "answers.jsonl", record)
        append(
            self.directory / "isolation.jsonl",
            {
                "id": q["id"],
                "erased": True,
                "memory_unchanged": True,
                "budget_usage_retained": all(
                    s.one("SELECT 1 FROM model_usage WHERE request_id=?", (u["request_id"],))
                    for u in usages
                ),
            },
        )
        print(
            f"exam {q['id']} prefix={self.imported} state={job['state']} calls={len(usages)}",
            flush=True,
        )
        return record
