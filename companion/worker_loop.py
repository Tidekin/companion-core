"""Completed chat-worker loop samples; neither a heartbeat nor business success."""

import json
import math
import re
import uuid
from datetime import datetime, timedelta
from time import monotonic

from companion.store import encode, utcnow

KEY = "worker_loop"
WRITE_INTERVAL_SECONDS = 2
MAX_AGE_SECONDS = 10
WAITING_STAGES = frozenset({"WAITING_AFTER_POLL", "WAITING_AFTER_MAINTENANCE"})
STAGES = frozenset({"POLL_COMPLETED", "POLL_AND_MAINTENANCE_COMPLETED"}) | WAITING_STAGES


class Recorder:
    def __init__(self, store):
        self.store = store
        self.session = uuid.uuid4().hex
        self.sequence = 0
        self.last_write = None
        self.last_waiting = None

    def completed(self, *, maintenance=False, waiting=False):
        self.sequence += 1
        if self.sequence >= 2**63:
            raise ValueError("WORKER_LOOP_SEQUENCE_EXHAUSTED")
        now = monotonic()
        if self.last_write is not None and self.last_waiting == waiting and (
            waiting and not maintenance or now - self.last_write < WRITE_INTERVAL_SECONDS
        ):
            return
        stage = (
            "WAITING_AFTER_MAINTENANCE" if maintenance else "WAITING_AFTER_POLL"
        ) if waiting else (
            "POLL_AND_MAINTENANCE_COMPLETED" if maintenance else "POLL_COMPLETED"
        )
        record = {"session": self.session, "sequence": self.sequence,
                  "completed_at": utcnow(), "stage": stage}
        self.store.execute(
            'INSERT INTO runtime_meta (key, value) VALUES (%s, %s) ON CONFLICT(key) DO UPDATE SET '
            'value = excluded.value, record_sequence = DEFAULT', (KEY, encode(record)),
        )
        self.last_write = now
        self.last_waiting = waiting


def _project(record, now):
    if (not isinstance(record, dict)
        or set(record) != {"session", "sequence", "completed_at", "stage"}
        or type(record["session"]) is not str
        or not re.fullmatch(r"[a-f0-9]{32}", record["session"])
        or type(record["sequence"]) is not int or not 1 <= record["sequence"] < 2**63
        or type(record["completed_at"]) is not str or len(record["completed_at"]) > 40
        or type(record["stage"]) is not str or record["stage"] not in STAGES):
        raise ValueError("WORKER_LOOP_RECORD_INVALID")
    try:
        completed = datetime.fromisoformat(record["completed_at"])
    except ValueError:
        raise ValueError("WORKER_LOOP_RECORD_INVALID") from None
    if completed.tzinfo is None or completed.utcoffset() != timedelta(0):
        raise ValueError("WORKER_LOOP_RECORD_INVALID")
    age = (now - completed).total_seconds()
    if age < -2:
        raise ValueError("WORKER_LOOP_RECORD_INVALID")
    return {"state": ("WAITING" if record["stage"] in WAITING_STAGES else "RECORDED")
            if age <= MAX_AGE_SECONDS else "STALE",
            "session": record["session"], "sequence": record["sequence"],
            "completed_at": completed.isoformat(timespec="microseconds"), "stage": record["stage"],
            "age_seconds": round(max(0, age), 6)}


def snapshot(view, now):
    row = view.one('SELECT CASE WHEN LENGTH(value) <= 1024 THEN value END AS value FROM '
        'runtime_meta WHERE key = %s', (KEY,))
    if row is None:
        return {"state": "UNOBSERVED"}
    try:
        if type(row["value"]) is not str:
            raise ValueError("WORKER_LOOP_RECORD_INVALID")
        return _project(json.loads(row["value"]), now)
    except (ValueError, TypeError, OverflowError):
        return {"state": "INVALID"}


def project_public(value, observed):
    """Validate the HTTP contract, reconstructing only fixed safe fields."""
    if not isinstance(value, dict) or type(value.get("state")) is not str:
        raise ValueError("WORKER_LOOP_RECORD_INVALID")
    if set(value) == {"state"} and value["state"] in {"UNOBSERVED", "INVALID"}:
        return {"state": value["state"]}
    if set(value) != {"state", "session", "sequence", "completed_at", "stage", "age_seconds"}:
        raise ValueError("WORKER_LOOP_RECORD_INVALID")
    age = value["age_seconds"]
    if type(age) not in {int, float} or not 0 <= age < 2**63 or not math.isfinite(age):
        raise ValueError("WORKER_LOOP_RECORD_INVALID")
    projected = _project({key: value[key] for key in (
        "session", "sequence", "completed_at", "stage",
    )}, observed)
    if projected != value:
        raise ValueError("WORKER_LOOP_RECORD_INVALID")
    return projected
