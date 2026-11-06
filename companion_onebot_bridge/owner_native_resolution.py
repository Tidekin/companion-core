"""Native proofs using the existing exclusive Spool and owner-resolution projection."""

import json
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import companion_delivery_protocol as wire
from companion_delivery_protocol import no_io

from . import reply_segments
from .onebot_client import bounded_post
from .owner_resolution import ACCOUNT_BINDING_FIELDS, BINDING_FIELDS, TABLE, UNCERTAIN
from .spool import encode


def binding(guard, item):
    if item["action_type"] in wire.ACCOUNT_ACTIONS:
        from .account_actions import send_check_owner

        return {"action_type": item["action_type"], "content_sha256": item["content_sha256"],
            "payload_sha256": item["content"]["payload_sha256"],
            "payload_kind": "account_payload_sha256", "account_plan_id": item["content"]["plan_id"],
            "authorization_sha256": item["content"]["authorization_sha256"],
            "lease_sha256": wire.digest(item["lease_token"]),
            "send_check_sha256": wire.digest(send_check_owner(guard.authority, item))}
    event_id = reply_segments.event_id(item)
    native = item["action_type"] == "qq.private.poke"
    kind = "content_sha256" if native else "reply_sha256"
    check = {"authority": guard.authority, "event_id": event_id,
        "message_id": item["message_id"], "lease_token": item["lease_token"],
        "content_sha256": wire.digest(item["content"])}
    revision = item.get("reply_revision")
    if revision is not None:
        check["reply_revision"] = revision
    if guard.prepared_voice is not None:
        check["voice"] = guard.prepared_voice.check
        revision = item["voice"]["binding"]["reply_revision"]
        check["reply_revision"] = revision
    return {"action_type": item["action_type"], "content_sha256": check["content_sha256"],
        "payload_kind": kind,
        "payload_sha256": wire.digest(item["content"] if native else item["content"]["text"]),
        "lease_sha256": wire.digest(item["lease_token"]),
        "send_check_sha256": wire.digest(check), "reply_revision": revision,
        **({"voice_check": check["voice"]} if "voice" in check else {})}


def rows(runtime):
    if not runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        return []
    cursor = runtime.db.execute(
        f"SELECT * FROM {TABLE} WHERE json_extract(proof_json,'$.version')=2")
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor]


class _Prefix:
    """Only the original records preceding the proof currently being checked."""

    def __init__(self, records=()):
        self.attempts = {}
        self.stop = self.confirmation = self.previous = None
        for record in records:
            self.append(record)

    def append(self, record):
        kind, value = record["kind"], record["data"]
        if kind in UNCERTAIN | {"ACK"}:
            message_id = value.get("message_id")
            if isinstance(message_id, str):
                self.attempts.setdefault(message_id, []).append(record)
        elif kind == "OWNER_STOPPED":
            self.stop = record
        elif kind == "CORE_STOP_CONFIRMED":
            self.confirmation = record
        self.previous = record


def _source(runtime, prefix, message_id):
    entries = prefix.attempts.get(message_id, [])
    reserved = [record for record in entries if record["kind"] == "RESERVED"]
    if len(reserved) != 1 or entries[-1]["kind"] not in UNCERTAIN:
        raise ValueError("DELIVERY_UNCERTAIN_SOURCE_REQUIRED")
    value = reserved[0]["data"]
    original = value.get("delivery_binding")
    account = isinstance(original, dict) and original.get("action_type") in wire.ACCOUNT_ACTIONS
    if (not isinstance(original, dict)
        or set(original) not in ((ACCOUNT_BINDING_FIELDS,) if account else (
            BINDING_FIELDS, BINDING_FIELDS | {"voice_check"}))
        or account and original["account_plan_id"] != value["event_id"]
        or original["content_sha256"] != value["text_hash"]):
        raise ValueError("DELIVERY_ORIGINAL_BINDING_REQUIRED")
    result = runtime.db.execute("SELECT body FROM results WHERE message_id=?",
                                (message_id,)).fetchone()
    body = json.loads(result[0]) if result else None
    if (isinstance(body, dict) and body.get("result") == "NOT_SENT"
        and isinstance(body.get("core_receipt"), dict)
        and body["core_receipt"].get("kind") == no_io.KIND):
        receipt = no_io.receipt(body["core_receipt"])
        proof = receipt["quiescence"]
        if (entries[-1]["kind"] != "RESERVED" or proof["message_id"] != message_id
            or proof["authority"] != runtime.authority
            or proof["reserved_record_sha256"] != wire.digest(reserved[0])
            or any(proof[key] != item for key, item in original.items())
            or body != {"result": "NOT_SENT", "lease_sha256": original["lease_sha256"],
                        "core_receipt": receipt}):
            raise ValueError("DELIVERY_ORIGINAL_BINDING_REQUIRED")
        # This accepted result was appended after the quiescence proof. It is not
        # an original provider result and cannot change that proof's source hash.
        body = None
    if body is not None:
        check = {"authority": runtime.authority, "event_id": value["event_id"],
            "message_id": message_id, "lease_token": body.get("lease_token"),
            "content_sha256": original["content_sha256"]}
        if account:
            check.pop("event_id")
            check["payload_sha256"] = original["payload_sha256"]
            receipt = body.get("account_action_receipt")
            if (not isinstance(receipt, dict) or receipt.get("result") != "UNKNOWN"
                or any(receipt.get(k) != v for k, v in {
                    "action_type": original["action_type"], "message_id": message_id,
                    "lease_token": body.get("lease_token"),
                    "payload_sha256": original["payload_sha256"]}.items())):
                raise ValueError("DELIVERY_ORIGINAL_BINDING_REQUIRED")
        elif original["reply_revision"] is not None:
            check["reply_revision"] = original["reply_revision"]
        if "voice_check" in original:
            check["voice"] = original["voice_check"]
        if (body.get("result") != "DELIVERY_UNCERTAIN"
            or body.get("provider_message_id") is not None or body.get("native_ack") is not None
            or wire.digest(body.get("lease_token")) != original["lease_sha256"]
            or wire.digest(check) != original["send_check_sha256"]):
            raise ValueError("DELIVERY_ORIGINAL_BINDING_REQUIRED")
    return {"authority": runtime.authority, "message_id": message_id,
        **({} if account else {"event_id": value["event_id"]}), "fence": value["fence"],
        "item_sha256": value["item_hash"], "attempt_state": entries[-1]["kind"],
        "attempt_record_sha256": wire.digest(entries[-1]),
        "reserved_record_sha256": wire.digest(reserved[0]),
        "original_result_sha256": wire.digest(body) if body is not None else None, **original}


def stop_chain(authority, stopped):
    """Core's STOP for `authority`, or the last STOP of a Core-proven unbound chain after it.

    The direct STOP keeps the same policy; Core only reports a longer chain once every
    intermediate ACTIVE revision has bound no work.
    """
    if not isinstance(stopped, dict) or set(stopped) != set(authority):
        return False
    distance = stopped["revision"] - authority["revision"]
    return (type(stopped["revision"]) is int and distance > 0 and distance % 2 == 1
            and all(stopped[key] == authority[key]
                    for key in ("version", "grant_id", "restore_generation"))
            and (distance > 1 or stopped["policy_sha256"] == authority["policy_sha256"]))


def _confirmation(runtime, value):
    expected = {"authority", "state", "stopped_authority", "core_stop_sha256",
                "budget_journal_id", "quiescence_proven"}
    if (not isinstance(value, dict) or set(value) != expected
        or value["authority"] != runtime.authority
        or not stop_chain(runtime.authority, value["stopped_authority"])
        or value["state"] != "STOPPED" or value["quiescence_proven"] is not False
        or not wire.hash_value(value["core_stop_sha256"])
        or not wire.identifier(value["budget_journal_id"], r"budget_[a-f0-9]{32}")):
        raise ValueError("OWNER_CORE_STOP_RECEIPT_INVALID")


def _check_proof(runtime, prefix, proof):
    wire.validate_quiescence(proof)
    source = _source(runtime, prefix, proof["message_id"])
    stop, confirmation = prefix.stop, prefix.confirmation
    if stop is None or confirmation is None:
        raise ValueError("DELIVERY_QUIESCENT_PROCESS_REQUIRED")
    confirmed = confirmation["data"]
    if (proof["version"] != 2 or any(proof[key] != value for key, value in source.items())
        or proof["budget_journal_id"] != confirmed["budget_journal_id"]
        or proof["core_stop_record_sha256"] != confirmed["core_stop_sha256"]
        or proof["stop_record_sha256"] != wire.digest(stop)
        or proof["bridge_journal_head_sha256"] != wire.digest(prefix.previous)
        or proof["stopped_at"] != stop["at"]
        or wire.timestamp(proof["quiesced_at"]) < wire.timestamp(confirmation["at"])):
        raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(runtime.config.capture_signing_key))
    key.public_key().verify(bytes.fromhex(proof["signature"]), wire.signing_payload(proof))


def verify(runtime, records):
    prepared, accepted = {}, {}
    if runtime.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,),
    ).fetchone() and not runtime.legacy and runtime.db.execute(
        f"SELECT 1 FROM {TABLE} WHERE json_extract(proof_json,'$.version')!=2 LIMIT 1",
    ).fetchone():
        raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")
    stopped = False
    context = runtime
    prefix = _Prefix()
    for record in records:
        kind, value = record["kind"], record["data"]
        if kind in {"OWNER_BOUND", "OWNER_REBOUND"}:
            context = SimpleNamespace(authority=value["authority"], db=runtime.db,
                                      config=runtime.config)
            stopped = False
        elif kind == "OWNER_STOPPED":
            stopped = True
        elif kind == "CORE_STOP_CONFIRMED":
            if not stopped:
                raise ValueError("DELIVERY_QUIESCENT_PROCESS_REQUIRED")
            _confirmation(context, value)
        elif kind == "DELIVERY_QUIESCED":
            _check_proof(context, prefix, value)
            if value["message_id"] in prepared:
                raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")
            prepared[value["message_id"]] = value
        elif kind == "OWNER_RECONCILED":
            receipt = wire.validate_receipt(value)
            message_id = receipt["delivery"]["message_id"]
            if (message_id in accepted or receipt["quiescence"] != prepared.get(message_id)
                or wire.timestamp(record["at"]) < wire.timestamp(receipt["at"])):
                raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")
            accepted[message_id] = (receipt, wire.digest(record))
        elif kind == "NOT_SENT" and value.get("core_receipt", {}).get("kind") == no_io.KIND:
            receipt = no_io.receipt(value["core_receipt"])
            message_id = receipt["delivery"]["message_id"]
            if (message_id in accepted or receipt["quiescence"] != prepared.get(message_id)
                or wire.timestamp(record["at"]) < wire.timestamp(receipt["at"])):
                raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")
            accepted[message_id] = (receipt, wire.digest(record))
        # Keep the global prefix across authority handoffs, and advance only after
        # checking this record: later ACKs, STOPs or proofs cannot alter its source.
        prefix.append(record)
    projected = {row["message_id"]: row for row in rows(runtime)}
    if set(projected) != set(prepared):
        raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")
    for message_id, proof in prepared.items():
        row = projected[message_id]
        receipt = json.loads(row["receipt_json"]) if row["receipt_json"] else None
        if (row["plan_hash"] != wire.digest(proof["authority"])
            or json.loads(row["proof_json"]) != proof
            or (receipt, row["receipt_record_sha256"]) != accepted.get(message_id, (None, None))):
            raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")


def _stopped(runtime):
    records = runtime.verify(active=False)
    if (not runtime.spool.reconciliation_only or runtime.spool.lock is None
        or runtime.row()["state"] != "STOPPED" or runtime.stop_path.is_symlink()
        or not runtime.stop_path.is_file() or runtime.stop_path.read_bytes() != b"OWNER_STOP\n"
        or wire.timestamp(runtime.spool.quiesced_at) > runtime.clock()):
        raise ValueError("DELIVERY_QUIESCENT_PROCESS_REQUIRED")
    return records, runtime.row()["journal_head"]


def _saved_ack(runtime, records, message_id):
    saved = runtime.db.execute("SELECT body,state FROM results WHERE message_id=?",
                               (message_id,)).fetchone()
    body = json.loads(saved[0]) if saved else None
    if not isinstance(body, dict) or body.get("result") != "SENT":
        return None
    entries = [record for record in records if record["kind"] in UNCERTAIN | {"ACK", "NOT_SENT"}
               and record["data"].get("message_id") == message_id]
    kinds = [record["kind"] for record in entries]
    if kinds not in (["RESERVED", "IO_STARTED"], ["RESERVED", "IO_STARTED", "ACK"]):
        raise ValueError("DELIVERY_SAVED_ACK_BINDING_REQUIRED")
    reserved = entries[0]["data"]
    original = reserved.get("delivery_binding")
    provider = body.get("provider_message_id")
    if (saved[1] not in {"pending", "accepted"}
        or set(body) not in ({"lease_token", "result", "provider_message_id"},
                            {"lease_token", "result", "provider_message_id", "error_code"})
        or "error_code" in body and body["error_code"] != "IMAGE_PREFLIGHT_SKIPPED"
        or not isinstance(provider, str) or not 1 <= len(provider) <= 128
        or not wire.identifier(body.get("lease_token"), r"lease_[a-f0-9]{32}")
        or reserved["plan_hash"] != wire.digest(runtime.authority)
        or not isinstance(original, dict) or set(original) != BINDING_FIELDS
        or original["action_type"] not in {"qq.private.reply", "qq.private.proactive"}
        or original["payload_kind"] != "reply_sha256"
        or original["content_sha256"] != reserved["text_hash"]
        or original["lease_sha256"] != wire.digest(body["lease_token"])
        or any(not wire.hash_value(original[key]) for key in (
            "content_sha256", "payload_sha256", "send_check_sha256"))):
        raise ValueError("DELIVERY_SAVED_ACK_BINDING_REQUIRED")
    check = {"authority": runtime.authority, "event_id": reserved["event_id"],
        "message_id": message_id, "lease_token": body["lease_token"],
        "content_sha256": original["content_sha256"]}
    if original["reply_revision"] is not None:
        check["reply_revision"] = original["reply_revision"]
    resolution = None
    if runtime.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        resolution = runtime.db.execute(f"SELECT 1 FROM {TABLE} WHERE message_id=?",
                                        (message_id,)).fetchone()
    if (wire.digest(check) != original["send_check_sha256"]
        or kinds[-1] == "ACK" and (saved[1] != "accepted"
            or entries[-1]["data"]["provider_ack_hash"] != wire.digest(provider))
        or resolution is not None):
        raise ValueError("DELIVERY_SAVED_ACK_BINDING_REQUIRED")
    return {"body": body, "state": saved[1], "attempt_state": kinds[-1],
            "provider_ack_sha256": wire.digest(provider)}


async def _recover_saved_ack(runtime, client, records, message_id):
    original = _saved_ack(runtime, records, message_id)
    if original is None:
        return None
    _, before = _stopped(runtime)
    # Replay only the durable provider receipt through Core's existing idempotent
    # result endpoint. This path never constructs a OneBot client or sends again.
    result = await bounded_post(client, f"/internal/v1/deliveries/{message_id}/result",
                                original["body"])
    records, head = _stopped(runtime)
    if (result != {"state": "SENT"} or head != before
        or _saved_ack(runtime, records, message_id) != original):
        raise ValueError("DELIVERY_SAVED_ACK_CONFIRMATION_REQUIRED")
    if original["attempt_state"] != "ACK":
        with runtime.db:
            runtime.append("ACK", {"message_id": message_id,
                                   "provider_ack_hash": original["provider_ack_sha256"]})
            runtime.db.execute("UPDATE window_attempts SET state='ACK',provider_ack_hash=? "
                "WHERE message_id=?", (original["provider_ack_sha256"], message_id))
            runtime.db.execute("UPDATE results SET state='accepted' WHERE message_id=?",
                               (message_id,))
    runtime.verify(active=False)
    return {"state": "ACK", "message_id": message_id, "paused": True,
            "provider_ack_sha256": original["provider_ack_sha256"], "core_result_confirmed": True}


async def prepare(runtime, client, message_id):
    _, before = _stopped(runtime)
    confirmation = await bounded_post(client, "/internal/v1/owner-authority/stop-receipt",
                                       {"authority": runtime.authority})
    _confirmation(runtime, confirmation)
    records, head = _stopped(runtime)
    if before != head:
        raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")
    previous = [record for record in records if record["kind"] == "CORE_STOP_CONFIRMED"
                and record["data"]["authority"] == runtime.authority]
    if previous and previous[-1]["data"] != confirmation:
        raise ValueError("OWNER_CORE_STOP_RECEIPT_INVALID")
    if not previous:
        with runtime.db:
            runtime.append("CORE_STOP_CONFIRMED", confirmation)
        records, head = _stopped(runtime)
    if acknowledged := await _recover_saved_ack(runtime, client, records, message_id):
        return acknowledged
    stored = next((row for row in rows(runtime) if row["message_id"] == message_id), None)
    if stored:
        proof = json.loads(stored["proof_json"])
    else:
        prefix = _Prefix(records)
        stop = prefix.stop
        proof = {"kind": "owner_delivery_quiescence_v2", "version": 2,
            **_source(runtime, prefix, message_id),
            "budget_journal_id": confirmation["budget_journal_id"],
            "stop_record_sha256": wire.digest(stop),
            "core_stop_record_sha256": confirmation["core_stop_sha256"],
            "bridge_journal_head_sha256": head, "stopped_at": stop["at"],
            "quiesced_at": runtime.clock().isoformat()}
        key = Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(runtime.config.capture_signing_key))
        proof["signature"] = key.sign(wire.signing_payload(proof)).hex()
        _check_proof(runtime, prefix, proof)
        runtime.db.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (message_id TEXT PRIMARY KEY,"
            "plan_hash TEXT NOT NULL,proof_json TEXT NOT NULL,receipt_json TEXT,"
            "receipt_record_sha256 TEXT)")
        with runtime.db:
            runtime.append("DELIVERY_QUIESCED", proof)
            runtime.db.execute(f"INSERT INTO {TABLE} VALUES (?,?,?,NULL,NULL)",
                (message_id, wire.digest(runtime.authority), encode(proof)))
    _, before = _stopped(runtime)
    result = await bounded_post(client, "/internal/v1/deliveries/reconciliation/prepare", proof)
    if _stopped(runtime)[1] != before:
        raise ValueError("DELIVERY_PREPARATION_RECEIPT_INVALID")
    if isinstance(result, dict) and result.get("state") == "NOT_SENT":
        if set(result) != {"state", "message_id", "receipt"} or result["message_id"] != message_id:
            raise ValueError("DELIVERY_NOT_SENT_RECEIPT_INVALID")
        return _accept_not_sent(runtime, proof, result["receipt"])
    if result != {
        "state": "READY", "message_id": message_id, "quiescence_sha256": wire.digest(proof),
    }:
        raise ValueError("DELIVERY_PREPARATION_RECEIPT_INVALID")
    return result | {"paused": True}


def _accept_not_sent(runtime, proof, value):
    receipt = no_io.receipt(value)
    message_id = proof["message_id"]
    if receipt["quiescence"] != proof or wire.timestamp(receipt["at"]) > runtime.clock():
        raise ValueError("DELIVERY_NOT_SENT_RECEIPT_INVALID")
    stored = next(row for row in rows(runtime) if row["message_id"] == message_id)
    if stored["receipt_json"] is not None:
        if json.loads(stored["receipt_json"]) != receipt:
            raise ValueError("DELIVERY_NOT_SENT_RECEIPT_INVALID")
    else:
        if runtime.db.execute("SELECT 1 FROM results WHERE message_id=?", (message_id,)).fetchone():
            raise ValueError("DELIVERY_ORIGINAL_BINDING_REQUIRED")
        attempt = runtime.db.execute("SELECT state FROM window_attempts WHERE message_id=?",
                                     (message_id,)).fetchone()
        if not attempt or attempt[0] != "RESERVED":
            raise ValueError("DELIVERY_NOT_SENT_PROOF_REQUIRED")
        runtime.spool.capacity(len(encode(receipt).encode()) * 3)
        with runtime.db:
            head = runtime.append("NOT_SENT", {"message_id": message_id,
                "provider_ack_hash": None, "core_receipt": receipt})
            runtime.db.execute("UPDATE window_attempts SET state='NOT_SENT' WHERE message_id=?",
                               (message_id,))
            # The original plaintext lease may be gone after Core expiry. Keep its
            # signed hash; never synthesize a token or queue a delivery-result POST.
            runtime.db.execute("INSERT INTO results(message_id,body,state) VALUES (?,?,'accepted')",
                (message_id, encode({"result": "NOT_SENT", "lease_sha256": proof["lease_sha256"],
                                     "core_receipt": receipt})))
            runtime.db.execute(f"UPDATE {TABLE} SET receipt_json=?,receipt_record_sha256=? "
                               "WHERE message_id=?", (encode(receipt), head, message_id))
    runtime.verify(active=False)
    return {"state": "NOT_SENT", "message_id": message_id, "paused": True,
            "receipt_sha256": receipt["receipt_sha256"]}


async def reconcile(runtime, client, message_id):
    _, before = _stopped(runtime)
    stored = next((row for row in rows(runtime) if row["message_id"] == message_id), None)
    if not stored:
        raise ValueError("DELIVERY_QUIESCENCE_REQUIRED")
    proof = json.loads(stored["proof_json"])
    saved = json.loads(stored["receipt_json"]) if stored["receipt_json"] else None
    if saved is not None and saved.get("kind") == no_io.KIND:
        return _accept_not_sent(runtime, proof, saved)
    result = await bounded_post(client,
        f"/internal/v1/deliveries/{message_id}/reconciliation/lookup",
        {"proof_sha256": wire.digest(proof)})
    if _stopped(runtime)[1] != before:
        raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")
    if result == {"state": "NOT_FOUND"} and stored["receipt_json"] is None:
        return {"state": "AWAITING_OWNER", "message_id": message_id, "paused": True}
    if (not isinstance(result, dict) or set(result) != {"state", "receipt"}
        or result["state"] != "FOUND"):
        raise ValueError("DELIVERY_RESOLUTION_RECEIPT_INVALID")
    receipt = wire.validate_receipt(result["receipt"])
    if (receipt["quiescence"] != proof or wire.timestamp(receipt["at"]) > runtime.clock()
        or stored["receipt_json"] is not None and json.loads(stored["receipt_json"]) != receipt):
        raise ValueError("DELIVERY_RESOLUTION_RECEIPT_INVALID")
    if stored["receipt_json"] is None:
        with runtime.db:
            head = runtime.append("OWNER_RECONCILED", receipt)
            runtime.db.execute(f"UPDATE {TABLE} SET receipt_json=?,receipt_record_sha256=? "
                "WHERE message_id=?", (encode(receipt), head, message_id))
    runtime.verify(active=False)
    return {"state": "OWNER_RESOLVED", "message_id": message_id,
            "outcome": receipt["outcome"], "receipt_sha256": receipt["receipt_sha256"],
            "paused": True}


async def command(config, args):
    from .network import private_http_client
    from .owner_runtime import OwnerRuntime
    from .spool import Spool

    if not wire.identifier(args.message_id, r"msg_[a-f0-9]{32}"):
        raise ValueError("DELIVERY_MESSAGE_REQUIRED")
    with Spool(config, reconciliation_only=True) as spool:
        runtime = OwnerRuntime(spool)
        async with private_http_client(config.core_url, config.bridge_token) as client:
            operation = prepare if args.command == "owner-delivery-prepare" else reconcile
            return await operation(runtime, client, args.message_id)
