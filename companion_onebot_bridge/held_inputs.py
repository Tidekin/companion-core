"""Hold owner frames received while Core was unreachable; admit them only after Core is back.

A held frame is not a capture: it has no signature, no inbox row and no reference or media
read, and nothing reaches Core from it. The table keeps the original frame with its real
receive time and peer; the Bridge journal records only hashes (`INPUT_HELD`). Once Core
answers again the oldest frame is prepared on the live path with that original receive
time (authority check, recall check, reference reads, signature, inbox), so Core still
applies its own 30 s receive bound, authority activation time, policy expiry, recall,
erasure and retention checks. Every held frame ends in one `INPUT_HOLD_CLOSED` record:

- PROMOTED: captured as an ordinary `authenticated_onebot_ws` input;
- DUPLICATE: the same message was already captured another way (live or backfill);
- REJECTED: with a reason code (held too long, recalled, Core refused, OWNER STOP, ...).

The frame text is deleted when the hold closes. While any frame is held, newer frames are
held behind it so the owner's order is kept.
"""

import hashlib
import json
import re
from datetime import datetime

import httpx

from . import journal
from .capture import MAX_AGE_SECONDS, prepare_owner_input
from .event_normalizer import normalize
from .metrics import emit
from .spool import encode

# Ten minutes covers an API restart or a short host hiccup; past that the frame text should
# not sit in the Bridge without Core having accepted it. Core's retention bound is longer.
HOLD_MAX_SECONDS = 600
MAX_HELD = 200
RESULTS = frozenset({"PROMOTED", "DUPLICATE", "REJECTED"})
# Custody, capacity and runtime failures stop the Bridge with the frame still held; any
# other refusal belongs to this one frame and closes it.
KEEP_HELD = ("OWNER_JOURNAL", "OWNER_RUNTIME", "OWNER_AUTHORITY_BINDING", "OWNER_HANDOFF",
             "OWNER_LEGACY", "OWNER_CUSTODY", "OWNER_CAPTURE_PROJECTION", "SPOOL_",
             "PRIVATE_", "SYMLINK")
TABLE = ("CREATE TABLE IF NOT EXISTS held_inputs(event_id TEXT PRIMARY KEY,"
         "record_json TEXT NOT NULL,frame TEXT,state TEXT NOT NULL,reason TEXT)")


def transient(error):
    """Core or the protocol did not answer; a refusal (4xx) is not transient."""
    return (isinstance(error, httpx.TransportError)
            or isinstance(error, httpx.HTTPStatusError) and error.response.status_code >= 500)


def _exists(runtime):
    return runtime.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='held_inputs'").fetchone() is not None


def waiting(runtime):
    return _exists(runtime) and runtime.db.execute(
        "SELECT 1 FROM held_inputs WHERE state='HELD' LIMIT 1").fetchone() is not None


def counts(runtime):
    if not _exists(runtime):
        return {}
    return dict(runtime.db.execute("SELECT state,COUNT(*) FROM held_inputs GROUP BY state"))


def hold(runtime, frame, received_at, peer):
    """Keep one owner message frame without Core; False when it is not one or already known."""
    runtime.verify(include_records=False)
    event = normalize(frame, runtime.config, now=received_at.timestamp())
    if event is None:
        return False
    occurred = datetime.fromisoformat(event["occurred_at"])
    if not 0 <= (received_at - occurred).total_seconds() <= MAX_AGE_SECONDS:
        raise ValueError("OWNER_CAPTURE_TIME_CHANGED")
    event_id = event["event_id"]
    runtime.db.execute(TABLE)
    if (runtime.db.execute("SELECT 1 FROM window_inputs WHERE event_id=?", (event_id,)).fetchone()
            or runtime.db.execute("SELECT 1 FROM held_inputs WHERE event_id=?",
                                  (event_id,)).fetchone()):
        return False
    if runtime.db.execute("SELECT COUNT(*) FROM held_inputs WHERE state='HELD'"
                          ).fetchone()[0] >= min(MAX_HELD, runtime.config.max_pending):
        raise ValueError("HELD_INPUT_CAPACITY")
    text = frame if isinstance(frame, str) else frame.decode()
    record = {"authority": runtime.authority, "event_id": event_id,
              "frame_sha256": hashlib.sha256(text.encode()).hexdigest(),
              "received_at": received_at.isoformat(), "peer": list(peer)}
    runtime.spool.capacity(len(text.encode()) * 3)
    with runtime.db:
        runtime.append("INPUT_HELD", {"event_id": event_id,
                                      "hold_sha256": journal.digest(record)})
        runtime.db.execute("INSERT INTO held_inputs VALUES(?,?,?,'HELD',NULL)",
                           (event_id, encode(record), text))
    emit("owner_input_hold", result="HELD")
    return True


def _close(runtime, event_id, record, result, reason=None):
    with runtime.db:
        runtime.append("INPUT_HOLD_CLOSED", {"event_id": event_id, "result": result,
                                             "reason": reason})
        runtime.db.execute("UPDATE held_inputs SET state=?,reason=?,frame=NULL "
                           "WHERE event_id=? AND state='HELD'", (result, reason, event_id))
    held = (runtime.clock() - datetime.fromisoformat(record["received_at"])).total_seconds()
    emit("owner_input_hold", result=result, reason=reason, held_seconds=round(held, 1))


def _code(error):
    code = error.args[0] if error.args and isinstance(error.args[0], str) else ""
    return code if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code) else "INPUT_REJECTED"


async def promote_one(runtime, client, onebot):
    """Admit or close the oldest held frame; transient failures leave it held and raise."""
    runtime.verify(include_records=False)
    if not _exists(runtime):
        return False
    row = runtime.db.execute("SELECT event_id,record_json,frame FROM held_inputs "
                             "WHERE state='HELD' ORDER BY rowid LIMIT 1").fetchone()
    if row is None:
        return False
    event_id, record, frame = row[0], json.loads(row[1]), row[2]
    received_at = datetime.fromisoformat(record["received_at"])
    if (runtime.clock() - received_at).total_seconds() > HOLD_MAX_SECONDS:
        _close(runtime, event_id, record, "REJECTED", "HOLD_EXPIRED")
        return True
    if record["authority"] != runtime.authority:
        _close(runtime, event_id, record, "REJECTED", "AUTHORITY_CHANGED")
        return True
    prior = runtime.db.execute("SELECT capture_json FROM window_inputs WHERE event_id=?",
                               (event_id,)).fetchone()
    if prior is None:
        from . import owner_notices

        try:
            prepared = await prepare_owner_input(runtime, client, onebot, frame, received_at,
                                                 tuple(record["peer"]))
            if prepared is None:
                reason = ("RECALLED" if owner_notices.recalled(runtime, event_id)
                          else "NOT_OWNER_INPUT")
                _close(runtime, event_id, record, "REJECTED", reason)
                return True
            runtime.persist_capture(prepared["event"], frame, received_at=received_at,
                                    peer=tuple(record["peer"]), media=prepared["media"])
        except httpx.HTTPStatusError as error:
            if transient(error):
                raise
            # Core refused the authority or purpose: record why, then fail closed as live does.
            _close(runtime, event_id, record, "REJECTED", f"CORE_HTTP_{error.response.status_code}")
            raise
        except ValueError as error:
            if _code(error).startswith(KEEP_HELD):
                raise
            _close(runtime, event_id, record, "REJECTED", _code(error))
            return True
        prior = runtime.db.execute("SELECT capture_json FROM window_inputs WHERE event_id=?",
                                   (event_id,)).fetchone()
    # A crash between the capture and this record leaves the capture with the original time.
    same = prior and json.loads(prior[0]).get("received_at") == record["received_at"]
    _close(runtime, event_id, record, "PROMOTED" if same else "DUPLICATE")
    return True


def close_all(runtime, reason):
    """OWNER STOP: nothing held may be admitted under a later authority."""
    if not _exists(runtime):
        return 0
    rows = runtime.db.execute("SELECT event_id,record_json FROM held_inputs WHERE state='HELD' "
                              "ORDER BY rowid").fetchall()
    for event_id, record in rows:
        _close(runtime, event_id, json.loads(record), "REJECTED", reason)
    return len(rows)


def verify(runtime, records, *, references):
    held, closed = {}, {}
    for record in records:
        data = record["data"]
        if record["kind"] == "INPUT_HELD":
            if data["event_id"] in held:
                raise ValueError("OWNER_HELD_INPUT_PROJECTION_CHANGED")
            held[data["event_id"]] = data["hold_sha256"]
        elif record["kind"] == "INPUT_HOLD_CLOSED":
            if (data["event_id"] not in held or data["event_id"] in closed
                    or data["result"] not in RESULTS):
                raise ValueError("OWNER_HELD_INPUT_PROJECTION_CHANGED")
            closed[data["event_id"]] = (data["result"], data["reason"])
    actual, done = {}, {}
    if _exists(runtime):
        for event_id, record_json, frame, state, reason in runtime.db.execute(
                "SELECT event_id,record_json,frame,state,reason FROM held_inputs"):
            record = json.loads(record_json)
            if (record["event_id"] != event_id or record["authority"] not in references
                    or (state == "HELD") != (frame is not None)
                    or frame is not None
                    and hashlib.sha256(frame.encode()).hexdigest() != record["frame_sha256"]):
                raise ValueError("OWNER_HELD_INPUT_PROJECTION_CHANGED")
            actual[event_id] = journal.digest(record)
            if state != "HELD":
                done[event_id] = (state, reason)
    if (actual, done) != (held, closed):
        raise ValueError("OWNER_HELD_INPUT_PROJECTION_CHANGED")
