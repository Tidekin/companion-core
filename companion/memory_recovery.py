"""Explicit, once-only recovery of settled native extraction failures.

The shared checkpoint still owns execution. Failed records retain its source-bound
payload, not another queue or worker; normal scheduling never retries these records.
"""

import json
from copy import deepcopy

from companion import owner_authority
from companion.contracts import CoreError
from companion.memory import Memory
from companion.memory_authority import source_allowed
from companion.memory_context import MemoryContext
from companion.store import encode

PREFIX = "memory_failed_batch:"


def retain(core, record):
    """Called inside the checkpoint transaction, before terminal payload stripping."""
    batch = record["batch"]
    if batch.get("phase") != "FAILED":
        return
    key = PREFIX + record["batch_id"]
    old = core.store.one("SELECT value FROM runtime_meta WHERE key=?", (key,))
    # Repeated terminal persistence cannot overwrite the sole original payload.
    if old and json.loads(old["value"])["state"] != "QUEUED":
        return
    saved = deepcopy(record)
    saved.update(state="FAILED", failed_at=core.clock(),
                 recoveries=batch.get("recoveries", 0))
    if old and "recovery_request_id" in json.loads(old["value"]):
        saved["recovery_request_id"] = json.loads(old["value"])["recovery_request_id"]
    core.store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)", (key, encode(saved)))


def failures(store):
    return [{"entry_id": value["batch_id"], "state": value["state"],
             "error": value["batch"].get("error"), "failed_at": value["failed_at"],
             "source_count": len(value["source_event_ids"]),
             "recovery_available": value["state"] == "FAILED" and value["recoveries"] == 0}
            for row in store.rows("SELECT value FROM runtime_meta WHERE key GLOB ? ORDER BY key",
                                  (PREFIX + "*",))
            for value in [json.loads(row["value"])]]


def retry(core, batch_id, request_id, *, owner_check=None):
    """Schedule one fresh bounded attempt after owner authorization; no provider I/O."""
    from companion.memory_work import CHECKPOINT_KEY, TERMINAL_PHASES

    s, now = core.store, core.clock()
    with s.transaction():
        if owner_check:
            owner_check()
        row = s.one("SELECT value FROM runtime_meta WHERE key=?", (PREFIX + batch_id,))
        if not row:
            raise CoreError("MEMORY_FAILURE_NOT_FOUND", 404)
        record = json.loads(row["value"])
        if record.get("recovery_request_id") == request_id:
            return {"entry_id": batch_id, "state": record["state"]}
        if not request_id or record["recoveries"] or record["state"] != "FAILED":
            raise CoreError("MEMORY_RECOVERY_EXHAUSTED", 409)
        checkpoint = s.one("SELECT value FROM runtime_meta WHERE key=?", (CHECKPOINT_KEY,))
        if checkpoint and json.loads(checkpoint["value"])["batch"].get("phase") not in (
            TERMINAL_PHASES
        ):
            raise CoreError("MEMORY_WORK_BUSY", 409)
        if not core.settings.memory_enabled:
            raise CoreError("MEMORY_DISABLED", 409)
        batch = record["batch"]
        if owner_authority.required(s, core.settings):
            owner_authority.current(s, core.settings, core.clock, purpose="memory",
                                    authority=batch.get("authority"))
        if record["timezone"] != core.settings.timezone:
            raise CoreError("MEMORY_TIMEZONE_CHANGED", 409)
        for key in {batch.get("request_id"), batch.get("extraction_request_id")} - {None}:
            usage = s.one("SELECT state FROM model_usage WHERE request_id=?", (key,))
            # An unreserved preallocated intent proves no dispatch, exactly as native
            # crash recovery does. A retained extraction payload still needs its
            # original settled receipt; missing/unknown receipts cannot authorize it.
            extraction = key == batch.get("extraction_request_id")
            if (usage and usage["state"] != "SETTLED") or (extraction and not usage):
                raise CoreError("MEMORY_RECOVERY_UNSETTLED", 409)
        if not all(source_allowed(s, core.settings, core.clock, key, "memory")
                   for key in record["source_event_ids"]):
            raise CoreError("PRIVACY_CHANGED", 409)
        memory = Memory(s, core.settings, core.ledger)
        context = MemoryContext(memory)
        context.revalidate(batch["proof"], now)
        resumed = deepcopy(record)
        resumed["next_attempt_at"] = None
        work = resumed["batch"]
        work.update(phase="REVIEW" if "candidates" in work and not work.get("repair_required")
                    else "EXTRACT", recoveries=1,
                    review_attempts=0, attempt_key="memory_recovery:" + batch_id,
                    proof=context.pin(now, event_ids=record["source_event_ids"]))
        work.pop("request_id", None)
        work.pop("error", None)
        record.update(state="QUEUED", recoveries=1, recovery_request_id=request_id)
        for key in work["event_ids"]:
            s.execute("UPDATE events SET memory_processed=0 WHERE event_id=?", (key,))
        s.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)",
                  (CHECKPOINT_KEY, encode(resumed)))
        s.execute("UPDATE runtime_meta SET value=? WHERE key=?",
                  (encode(record), PREFIX + batch_id))
        s.audit("memory.recovery_requested", batch_id, "console", now)
        return {"entry_id": batch_id, "state": "QUEUED"}


def finish(store, record):
    if record["batch"].get("phase") not in {"COMPLETE", "CANCELLED", "UNKNOWN"}:
        return
    key = PREFIX + record["batch_id"]
    row = store.one("SELECT value FROM runtime_meta WHERE key=?", (key,))
    if row and json.loads(row["value"])["state"] == "QUEUED":
        saved = json.loads(row["value"])
        saved["state"] = record["batch"]["phase"]
        # A completed or uncertain recovery must not retain a replayable payload.
        saved["batch"] = {"phase": saved["state"], "error": record["batch"].get("error")}
        store.execute("UPDATE runtime_meta SET value=? WHERE key=?", (encode(saved), key))
