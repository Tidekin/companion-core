"""Native recall custody in the original durable notice queue and Bridge journal."""

import hashlib
import json
from datetime import datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion_delivery_protocol.authority import recall_signing_payload

from . import journal
from .event_normalizer import normalize_recall
from .onebot_client import bounded_post
from .spool import encode


def verify(runtime, records, *, references=None):
    references = references or [runtime.authority]
    received, accepted = {}, {}
    for record in records:
        data = record["data"]
        if record["kind"] == "RECALL_RECEIVED":
            if data["recall_id"] in received:
                raise ValueError("OWNER_RECALL_PROJECTION_CHANGED")
            received[data["recall_id"]] = data["receipt_sha256"]
        elif record["kind"] == "RECALL_ACCEPTED":
            if data["recall_id"] not in received or data["recall_id"] in accepted:
                raise ValueError("OWNER_RECALL_PROJECTION_CHANGED")
            accepted[data["recall_id"]] = data["response_sha256"]
    actual, done = {}, {}
    if runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='automatic_recalls'").fetchone():
        for key, authority, event_id, receipt_json, state, response_json in runtime.db.execute(
            "SELECT recall_id,authority_json,target_event_id,record_json,state,response_json "
            "FROM automatic_recalls WHERE authority_json IS NOT NULL"
        ):
            receipt = json.loads(receipt_json)
            payload = {k: v for k, v in receipt.items() if k != "signature"}
            public = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(
                runtime.config.capture_signing_key)).public_key()
            public.verify(bytes.fromhex(receipt["signature"]), recall_signing_payload(payload))
            if (receipt["recall_id"] != key or receipt["target_event_id"] != event_id
                or json.loads(authority) != receipt["authority"]
                or receipt["authority"] not in references
                or state not in {"PENDING", "ACCEPTED"}
                or (response_json is not None) != (state == "ACCEPTED")):
                raise ValueError("OWNER_RECALL_PROJECTION_CHANGED")
            actual[key] = journal.digest(receipt)
            if response_json is not None:
                done[key] = journal.digest(json.loads(response_json))
    if (actual, done) != (received, accepted):
        raise ValueError("OWNER_RECALL_PROJECTION_CHANGED")


def put(runtime, frame, received_at, peer):
    runtime.verify(include_records=False)
    value = normalize_recall(frame, runtime.config, now=received_at.timestamp())
    if value is None:
        return False
    if not 0 <= (received_at - datetime.fromisoformat(value["occurred_at"])).total_seconds() <= 30:
        raise ValueError("OWNER_RECALL_TIME_CHANGED")
    runtime.db.execute("CREATE TABLE IF NOT EXISTS automatic_recalls (recall_id TEXT PRIMARY KEY,"
        "authority_json TEXT NOT NULL,target_event_id TEXT NOT NULL,record_json TEXT NOT NULL,"
        "state TEXT NOT NULL,response_json TEXT)")
    if runtime.db.execute("SELECT 1 FROM automatic_recalls WHERE recall_id=?",
                          (value["recall_id"],)).fetchone():
        return False
    if runtime.db.execute("SELECT COUNT(*) FROM automatic_recalls WHERE state='PENDING'"
                          ).fetchone()[0] >= runtime.config.max_pending:
        raise ValueError("AUTOMATIC_RECALL_QUEUE_FULL")
    raw = frame.encode() if isinstance(frame, str) else frame
    receipt = {"kind": "authenticated_onebot_friend_recall", "authority": runtime.authority,
        **value, "protocol_epoch": runtime.config.protocol_epoch,
        "identity_sha256": journal.digest([runtime.config.owner_qq_id, runtime.config.bot_qq_id]),
        "received_at": received_at.isoformat(), "frame_sha256": hashlib.sha256(raw).hexdigest()}
    receipt["evidence_sha256"] = journal.digest({"receipt": dict(receipt), "peer": list(peer),
        "endpoint": runtime.config.onebot_ws_url, "bearer_accepted": True})
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(runtime.config.capture_signing_key))
    receipt["signature"] = key.sign(recall_signing_payload(receipt)).hex()
    runtime.spool.capacity(len(encode(receipt).encode()) * 3)
    with runtime.db:
        runtime.append("RECALL_RECEIVED", {"recall_id": value["recall_id"],
                                           "receipt_sha256": journal.digest(receipt)})
        runtime.db.execute("INSERT INTO automatic_recalls(recall_id,authority_json,target_event_id,"
            "record_json,state,response_json) VALUES(?,?,?,?,'PENDING',NULL)",
            (value["recall_id"], encode(runtime.authority), value["target_event_id"],
             encode(receipt)))
    return True


def recalled(runtime, event_id):
    """Read only authenticated, journal-verified recall custody."""
    runtime.verify(active=False, include_records=False)
    if not runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='automatic_recalls'"
                              ).fetchone():
        return False
    return runtime.db.execute("SELECT 1 FROM automatic_recalls WHERE target_event_id=? "
        "AND authority_json IS NOT NULL LIMIT 1", (event_id,)).fetchone() is not None


async def flush_one(runtime, client):
    runtime.verify(active=False, include_records=False)
    if not runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name='automatic_recalls'"
                              ).fetchone():
        return False
    row = runtime.db.execute("SELECT recall_id,record_json FROM automatic_recalls "
                             "WHERE state='PENDING' AND authority_json IS NOT NULL "
                             "ORDER BY rowid LIMIT 1").fetchone()
    if row is None:
        return False
    receipt = json.loads(row[1])
    result = await bounded_post(client, "/internal/v1/owner-authority/inputs/recall", receipt)
    if (set(result) != {"recall_id", "target_event_id", "state", "duplicate", "reply_revision"}
        or result["recall_id"] != row[0] or result["target_event_id"] != receipt["target_event_id"]
        or result["state"] not in {"RECALLED", "PENDING_TARGET"}
        or type(result["duplicate"]) is not bool or type(result["reply_revision"]) is not int):
        raise ValueError("OWNER_RECALL_RECEIPT_INVALID")
    runtime.verify(active=False, include_records=False)
    with runtime.db:
        runtime.append("RECALL_ACCEPTED", {"recall_id": row[0],
                                           "response_sha256": journal.digest(result)})
        runtime.db.execute("UPDATE automatic_recalls SET state='ACCEPTED',response_json=? "
                           "WHERE recall_id=?", (encode(result), row[0]))
    from . import pending_media

    pending_media.cancel(runtime, receipt["target_event_id"], result)
    return True
