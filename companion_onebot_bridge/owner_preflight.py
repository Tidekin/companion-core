"""Retire the original text reply only on positive pre-dispatch evidence."""

import asyncio
import json

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion_delivery_protocol import digest, preflight

from .onebot_client import PreflightBusy, bounded_post
from .spool import encode


class Expired(Exception):
    def __init__(self, stage):
        self.stage = stage


async def checked_post(guard, path, body):
    if not preflight.eligible(guard.current_item):
        return await bounded_post(guard.client, path, body)
    stage = path.rsplit("/", 1)[1]
    busy = False
    while True:
        if busy:
            if guard.runtime.clock() >= guard.lease_until:
                raise Expired(stage)
            try:
                guard._local()
            except ValueError:
                if guard.runtime.clock() >= guard.lease_until:
                    raise Expired(stage) from None
                raise
        try:
            return await bounded_post(guard.client, path, body, preflight=True)
        except PreflightBusy:
            busy = True
            remaining = (guard.lease_until - guard.runtime.clock()).total_seconds()
            if remaining > 0:
                await asyncio.sleep(min(0.1, remaining))
        except httpx.TransportError:
            # Core may have committed begin-send even though its ACK was lost.
            # Never repeat that POST or dispatch OneBot from an unknown response.
            # The same signed RESERVED witness can close the expired original lease.
            remaining = (guard.lease_until - guard.runtime.clock()).total_seconds()
            if remaining > 0:
                await asyncio.sleep(remaining)
            raise Expired(stage) from None
        except httpx.HTTPStatusError as error:
            if (busy and error.response.status_code == 409
                and guard.runtime.clock() >= guard.lease_until):
                raise Expired(stage) from error
            raise


def _reservation(runtime, message_id):
    records = runtime.verify(active=False)
    attempt = runtime.db.execute("SELECT state FROM window_attempts WHERE message_id=? "
        "AND plan_hash=?", (message_id, digest(runtime.authority))).fetchone()
    if not attempt or attempt[0] != "RESERVED":
        raise ValueError("OWNER_PREFLIGHT_RESERVATION_REQUIRED")
    return next(record for record in reversed(records) if record["kind"] == "RESERVED"
                and record["data"]["message_id"] == message_id)


def save(guard, item, stage):
    runtime = guard.runtime
    record = _reservation(runtime, item["message_id"])
    if (not preflight.eligible(item) or guard.dispatched or item is not guard.current_item
        or digest(item) != record["data"]["item_hash"] or runtime.clock() < guard.lease_until):
        raise ValueError("OWNER_PREFLIGHT_RESERVATION_REQUIRED")
    proof = {"kind": preflight.KIND, "authority": guard.authority, "stage": stage,
        "message_id": item["message_id"], "reserved_record_sha256": digest(record),
        "lease_sha256": digest(item["lease_token"]), "lease_until": item["lease_until"],
        "item_sha256": digest(item)}
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(runtime.config.capture_signing_key))
    proof["signature"] = key.sign(preflight.signing_bytes(proof)).hex()
    value = {"result": preflight.KIND, "lease_token": item["lease_token"], "proof": proof}
    previous = runtime.db.execute("SELECT body,state FROM results WHERE message_id=?",
                                  (item["message_id"],)).fetchone()
    if previous:
        fallback = {"result": "DELIVERY_UNCERTAIN", "lease_token": item["lease_token"],
                    "error_code": "BRIDGE_SEND_INTERRUPTED"}
        if previous[1] != "pending" or json.loads(previous[0]) != fallback or stage != "send-check":
            raise ValueError("OWNER_PREFLIGHT_RESERVATION_REQUIRED")
    elif stage != "begin-send":
        raise ValueError("OWNER_PREFLIGHT_RESERVATION_REQUIRED")
    runtime.spool.capacity(4096)
    with runtime.db:
        runtime.db.execute("INSERT OR REPLACE INTO results(message_id,body,state) "
                           "VALUES (?,?,'pending')", (item["message_id"], encode(value)))


def validate(runtime, message_id, value):
    record = _reservation(runtime, message_id)
    proof = value["proof"]
    if (value["result"] != preflight.KIND or proof["kind"] != preflight.KIND
        or proof["authority"] != runtime.authority or proof["message_id"] != message_id
        or proof["item_sha256"] != record["data"]["item_hash"]
        or proof["reserved_record_sha256"] != digest(record)
        or proof["lease_sha256"] != record["data"]["delivery_binding"]["lease_sha256"]
        or proof["lease_sha256"] != digest(value["lease_token"])):
        raise ValueError("OWNER_PREFLIGHT_RESERVATION_REQUIRED")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    Ed25519PublicKey.from_public_bytes(bytes.fromhex(runtime.config.capture_public_key)).verify(
        bytes.fromhex(proof["signature"]), preflight.signing_bytes(proof))


async def flush(runtime, client, message_id, value):
    validate(runtime, message_id, value)
    receipt = await bounded_post(client, "/internal/v1/owner-authority/expired-preflight",
                                 value["proof"])
    expected = {"cancelled": True, "message_id": message_id,
                "state": "CANCELLED", "send_attempt_retained": True}
    if receipt != expected:
        raise ValueError("OWNER_CANCEL_RECEIPT_INVALID")
    validate(runtime, message_id, value)
    with runtime.db:
        runtime.append("NOT_SENT", {"message_id": message_id, "provider_ack_hash": None,
                                    "core_receipt": receipt})
        runtime.db.execute("UPDATE window_attempts SET state='NOT_SENT' WHERE message_id=?",
                           (message_id,))
        runtime.db.execute("UPDATE results SET body=?,state='accepted' WHERE message_id=?",
            (encode({"result": "NOT_SENT", "lease_token": value["lease_token"],
                     "core_receipt": receipt}), message_id))
    runtime.verify(active=False)
