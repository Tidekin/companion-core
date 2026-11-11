"""Stopped-process proofs and authenticated owner decisions; never send retries."""

import json
from types import SimpleNamespace

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

import companion_delivery_protocol as wire

from .spool import Spool, encode

TABLE = "window_delivery_resolutions"
UNCERTAIN = {"RESERVED", "IO_STARTED", "UNKNOWN"}
BINDING_FIELDS = {"action_type", "content_sha256", "payload_sha256", "payload_kind",
                  "lease_sha256", "send_check_sha256", "reply_revision"}
ACCOUNT_BINDING_FIELDS = (BINDING_FIELDS - {"reply_revision"}) | {
    "account_plan_id", "authorization_sha256",
}


def binding(window, item, event_id):
    """Retain original HTTP authority hashes without retaining generated text."""
    if window.plan.mode != "live_text":
        return None
    if item["action_type"] in wire.ACCOUNT_ACTIONS:
        from .account_actions import send_check

        return {"action_type": item["action_type"], "content_sha256": item["content_sha256"],
            "payload_sha256": item["content"]["payload_sha256"],
            "payload_kind": "account_payload_sha256", "account_plan_id": event_id,
            "authorization_sha256": item["content"]["authorization_sha256"],
            "lease_sha256": wire.digest(item["lease_token"]),
            "send_check_sha256": wire.digest(send_check(window, item))}
    if item["action_type"] not in wire.PRIVATE_ACTIONS:
        return None
    native = item["action_type"] == "qq.private.poke"
    kind = "content_sha256" if native else "reply_sha256"
    payload = wire.digest(item["content"] if native else item["content"]["text"])
    check = {"window_sha256": window.plan.sha256, "message_id": item["message_id"],
             "event_id": event_id, kind: payload, "lease_token": item["lease_token"]}
    revision = item.get("reply_revision")
    if revision is not None:
        check["reply_revision"] = revision
    voice_check = None
    if "voice" in item:
        from .voice_runtime import prepare_delivery

        voice_check = prepare_delivery(window, item).check
        check["voice"] = voice_check
        revision = item["voice"]["binding"]["reply_revision"]
        check["reply_revision"] = revision
    return {"action_type": item["action_type"], "content_sha256": wire.digest(item["content"]),
            "payload_sha256": payload, "payload_kind": kind,
            "lease_sha256": wire.digest(item["lease_token"]),
            "send_check_sha256": wire.digest(check), "reply_revision": revision,
            **({"voice_check": voice_check} if voice_check is not None else {})}


def rows(window):
    if not window.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        return []
    return window.db.execute(f"SELECT * FROM {TABLE}").fetchall()


def resolved(window):
    return {row["message_id"] for row in rows(window) if row["receipt_json"] is not None}


def pending_result(spool, *, runtime=None):
    done = set()
    if spool.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        if spool.db.execute("SELECT 1 FROM sqlite_master WHERE name='owner_runtime'").fetchone():
            from .owner_runtime import OwnerRuntime

            # An active sender owns its existing runtime. Constructing another
            # one here would apply the startup fence to its in-flight attempt.
            if runtime is not None and runtime.spool is not spool:
                raise ValueError("OWNER_RECEIPT_SPOOL_REQUIRED")
            if runtime is not None and not spool.db.execute(
                "SELECT 1 FROM results WHERE state='pending' LIMIT 1").fetchone():
                return None
            if runtime is None:
                runtime = OwnerRuntime(spool)
            runtime.verify(active=False, include_records=False)
            done = {row[0] for row in spool.db.execute(
                f"SELECT message_id FROM {TABLE} WHERE receipt_json IS NOT NULL")}
            return next((row for row in spool.db.execute(
                "SELECT message_id,body FROM results WHERE state='pending' ORDER BY rowid")
                if row[0] not in done), None)
        from .window import TestWindow, stored_plan

        saved = spool.db.execute("SELECT plan_json FROM test_window WHERE singleton=1").fetchone()
        if not saved:
            raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")
        window = TestWindow(spool, stored_plan(saved[0], spool.config))
        window.verify(active=False)
        done = resolved(window)
    return next((row for row in spool.db.execute(
        "SELECT message_id,body FROM results WHERE state='pending' ORDER BY rowid")
        if row[0] not in done), None)


def unsettled_attempts(window):
    done = resolved(window)
    return [row for row in window.db.execute("SELECT * FROM window_attempts")
            if row["state"] not in {"ACK", "NOT_SENT"} and row["message_id"] not in done]


def unsettled_results(window):
    done = resolved(window)
    return [row for row in window.db.execute("SELECT * FROM results")
            if row["message_id"] not in done and (row["state"] != "accepted"
                or json.loads(row["body"]).get("result") not in {"SENT", "NOT_SENT"})]


def terminal_inputs(window, history=None):
    history = history or window.verify_history(window.read_journal()[0])
    return {entry["data"]["event_hash"] for _, entries in history for entry in entries
            if entry["kind"] == "OWNER_INPUT_RECONCILED"}


def unsettled_inputs(window):
    done = terminal_inputs(window)
    return [row for row in window.db.execute("SELECT * FROM window_inputs")
            if row["state"] not in {"ACK", "CANCELLED", "SUPERSEDED", "RECALLED"}
            and wire.digest(row["event_id"]) not in done]


def _closed_input(window, receipt, receipts, history):
    closure = receipt.get("input_closure")
    if not closure or not closure["terminal"]:
        return None
    event_id = receipt["delivery"]["event_id"]
    row = window.db.execute("SELECT * FROM window_inputs WHERE event_id=?", (event_id,)).fetchone()
    if row is None or row["state"] in {"ACK", "CANCELLED", "SUPERSEDED", "RECALLED"}:
        return None
    if (row["state"] != "APPROVED" or closure["event_id"] != event_id
        or closure["input_sha256"] != row["input_hash"]
        or row["plan_hash"] != receipt["quiescence"]["plan_sha256"]):
        raise ValueError("DELIVERY_INPUT_CLOSURE_REQUIRED")
    delivered = {item["message_id"]: item for item in closure["deliveries"]}
    attempts = {item["message_id"]: item for item in window.db.execute(
        "SELECT * FROM window_attempts WHERE event_id=?", (event_id,))}
    reserved = {(plan.sha256, entry["data"]["message_hash"]): entry["data"]
                for plan, entries in history for entry in entries if entry["kind"] == "RESERVED"}
    if not attempts or set(attempts) - set(delivered):
        raise ValueError("DELIVERY_INPUT_CLOSURE_REQUIRED")
    for message_id, item in delivered.items():
        attempt = attempts.get(message_id)
        if attempt is None:
            if item["attempt_count"] != 0 or item["state"] not in {"CANCELLED", "FAILED"}:
                raise ValueError("DELIVERY_INPUT_CLOSURE_REQUIRED")
            continue
        original = reserved.get((attempt["plan_hash"], wire.digest(message_id)))
        if (not original or original["item_sha256"] != attempt["item_hash"]
            or original["event_hash"] != wire.digest(event_id)
            or original["fence"] != attempt["fence"]):
            raise ValueError("DELIVERY_INPUT_CLOSURE_REQUIRED")
        content_hash = original.get("delivery_binding", {}).get("content_sha256")
        if content_hash is not None and content_hash != item["content_sha256"]:
            raise ValueError("DELIVERY_INPUT_CLOSURE_REQUIRED")
        result = window.db.execute("SELECT * FROM results WHERE message_id=?",
                                  (message_id,)).fetchone()
        body = json.loads(result["body"]) if result else {}
        if attempt["state"] == "ACK":
            acknowledgement = body.get("native_ack") or body.get("provider_message_id")
            provider_hash = wire.digest({"provider_message_id": body.get("provider_message_id"),
                                         "native_ack": body.get("native_ack")})
            if (not result or result["state"] != "accepted" or body.get("result") != "SENT"
                or item["state"] != "SENT" or item["attempt_count"] != 1
                or not acknowledgement or attempt["provider_ack_hash"] != wire.digest(
                    acknowledgement) or item["provider_receipt_sha256"] != provider_hash
                or item["owner_resolution_sha256"] is not None):
                raise ValueError("DELIVERY_INPUT_CLOSURE_REQUIRED")
        elif attempt["state"] == "NOT_SENT":
            if (not result or result["state"] != "accepted" or body.get("result") != "NOT_SENT"
                or item["state"] != "CANCELLED" or item["attempt_count"] not in {0, 1}
                or item["owner_resolution_sha256"] is not None):
                raise ValueError("DELIVERY_INPUT_CLOSURE_REQUIRED")
        else:
            resolution = receipts.get(message_id)
            if (attempt["state"] not in UNCERTAIN or resolution is None
                or item["state"] != resolution["outcome"]
                or any(item[field] != resolution["delivery"][field] for field in (
                    "message_id", "content_sha256", "authority_sha256", "attempt_count"))
                or item["owner_resolution_sha256"] != (None if message_id == receipt[
                    "delivery"]["message_id"] else resolution["record_sha256"])
                or item["provider_receipt_sha256"] != wire.digest({
                    "provider_message_id": None, "native_ack": None})):
                raise ValueError("DELIVERY_INPUT_CLOSURE_REQUIRED")
    return row


def _source(window, plan, entries, message_id):
    attempt = window.db.execute("SELECT * FROM window_attempts WHERE message_id=?",
                                (message_id,)).fetchone()
    if (not attempt or attempt["plan_hash"] != plan.sha256
        or attempt["state"] not in UNCERTAIN or attempt["provider_ack_hash"] is not None):
        raise ValueError("DELIVERY_UNCERTAIN_SOURCE_REQUIRED")
    records = [entry for entry in entries if entry["kind"] in UNCERTAIN | {"ACK", "NOT_SENT"}
               and entry["data"].get("fence") == attempt["fence"]]
    reserved = [entry for entry in records if entry["kind"] == "RESERVED"]
    if (len(reserved) != 1 or records[-1]["kind"] != attempt["state"]
        or reserved[0]["data"].get("message_hash") != wire.digest(message_id)
        or reserved[0]["data"].get("event_hash") != wire.digest(attempt["event_id"])
        or reserved[0]["data"].get("item_sha256") != attempt["item_hash"]):
        raise ValueError("DELIVERY_UNCERTAIN_SOURCE_REQUIRED")
    result = window.db.execute("SELECT * FROM results WHERE message_id=?", (message_id,)).fetchone()
    body = json.loads(result["body"]) if result else None
    original = reserved[0]["data"].get("delivery_binding")
    account_receipt = body.get("account_action_receipt") if body else None
    account = bool((original and original.get("action_type") in wire.ACCOUNT_ACTIONS)
                   or account_receipt)
    if body is not None and (body.get("result") != "DELIVERY_UNCERTAIN"
        or not isinstance(body.get("lease_token"), str) or not body["lease_token"]
        or body.get("provider_message_id") is not None or body.get("native_ack") is not None):
        raise ValueError("DELIVERY_UNCERTAIN_SOURCE_REQUIRED")
    if account and body is not None and (not isinstance(account_receipt, dict)
        or account_receipt.get("result") != "UNKNOWN"
        or account_receipt.get("action_type") not in wire.ACCOUNT_ACTIONS
        or account_receipt.get("message_id") != message_id
        or account_receipt.get("lease_token") != body["lease_token"]):
        raise ValueError("DELIVERY_UNCERTAIN_SOURCE_REQUIRED")
    if original is None and account:
        if (not wire.identifier(attempt["event_id"], r"social_[a-f0-9]{32}")
            or plan.value["live_text"].get("account_actions_scope") != "qq_social_v1"):
            raise ValueError("DELIVERY_ORIGINAL_BINDING_REQUIRED")
        check = {"window_sha256": plan.sha256, "message_id": message_id,
            "content_sha256": attempt["text_hash"],
            "payload_sha256": account_receipt["payload_sha256"], "lease_token": body["lease_token"]}
        original = {"action_type": account_receipt["action_type"],
            "content_sha256": attempt["text_hash"],
            "payload_sha256": account_receipt["payload_sha256"],
            "payload_kind": "account_payload_sha256", "account_plan_id": attempt["event_id"],
            "authorization_sha256": plan.value["live_text"][
                "account_actions_authorization_sha256"],
            "lease_sha256": wire.digest(body["lease_token"]),
            "send_check_sha256": wire.digest(check)}
    elif original is None:
        # Old native poke records have a unique canonical payload and admitted origin.
        # Legacy generated-text revisions cannot safely be inferred from a text hash.
        from . import pokes

        content = {"kind": "poke", "target_user_id": plan.config.owner_qq_id}
        if (body is None or attempt["text_hash"] != wire.digest(content)
            or not pokes.admitted(SimpleNamespace(db=window.db, plan=plan), attempt["event_id"])):
            raise ValueError("DELIVERY_ORIGINAL_BINDING_REQUIRED")
        check = {"window_sha256": plan.sha256, "message_id": message_id,
                 "event_id": attempt["event_id"], "content_sha256": attempt["text_hash"],
                 "lease_token": body["lease_token"]}
        original = {"action_type": "qq.private.poke", "content_sha256": attempt["text_hash"],
            "payload_sha256": attempt["text_hash"], "payload_kind": "content_sha256",
            "lease_sha256": wire.digest(body["lease_token"]),
            "send_check_sha256": wire.digest(check), "reply_revision": None}
    if (not isinstance(original, dict)
        or set(original) not in ((ACCOUNT_BINDING_FIELDS,) if account else (
            BINDING_FIELDS, BINDING_FIELDS | {"voice_check"}))
        or original["action_type"] not in wire.ACTIONS
        or original["content_sha256" if account else "payload_sha256"] != attempt["text_hash"]
        or account and (original["account_plan_id"] != attempt["event_id"]
            or original["authorization_sha256"] != plan.value["live_text"].get(
                "account_actions_authorization_sha256"))):
        raise ValueError("DELIVERY_ORIGINAL_BINDING_REQUIRED")
    if body is not None:
        check = {"window_sha256": plan.sha256, "message_id": message_id,
                 "lease_token": body["lease_token"]}
        if account:
            check.update(content_sha256=original["content_sha256"],
                         payload_sha256=original["payload_sha256"])
            if (account_receipt["payload_sha256"] != original["payload_sha256"]
                or account_receipt["action_type"] != original["action_type"]):
                raise ValueError("DELIVERY_ORIGINAL_BINDING_REQUIRED")
        else:
            check.update(event_id=attempt["event_id"],
                         **{original["payload_kind"]: original["payload_sha256"]})
        if original.get("reply_revision") is not None:
            check["reply_revision"] = original["reply_revision"]
        if "voice_check" in original:
            check["voice"] = original["voice_check"]
        if (wire.digest(body["lease_token"]) != original["lease_sha256"]
            or wire.digest(check) != original["send_check_sha256"]):
            raise ValueError("DELIVERY_ORIGINAL_BINDING_REQUIRED")
    return {"window_id": plan.value["window_id"], "plan_sha256": plan.sha256,
        "budget_journal_id": plan.value["live_text"]["budget_journal_id"],
        "message_id": message_id, **({} if account else {"event_id": attempt["event_id"]}),
        "fence": attempt["fence"],
        "item_sha256": attempt["item_hash"], "attempt_state": attempt["state"],
        "attempt_record_sha256": wire.digest(records[-1]),
        "reserved_record_sha256": wire.digest(reserved[0]),
        "original_result_sha256": wire.digest(body) if body is not None else None, **original}


def _check_proof(window, plan, entries, proof):
    wire.validate_quiescence(proof)
    original = _source(window, plan, entries, proof["message_id"])
    stops = [entry for entry in entries if entry["kind"] == "STOP"]
    confirmations = [entry for entry in entries if entry["kind"] == "CORE_STOP_CONFIRMED"]
    if (not stops or not confirmations
        or any(proof[key] != value for key, value in original.items())
        or proof["stop_record_sha256"] != wire.digest(stops[-1])
        or proof["core_stop_record_sha256"] != wire.digest(confirmations[-1])
        or proof["bridge_journal_head_sha256"] != wire.digest(entries[-1])
        or proof["stopped_at"] != stops[-1]["at"]
        or wire.timestamp(proof["quiesced_at"]) < wire.timestamp(confirmations[-1]["at"])):
        raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(plan.value["live_text"][
        "capture_public_key"])).verify(bytes.fromhex(proof["signature"]),
                                      wire.signing_payload(proof))


def check_history(window, history):
    """Every projection must match its original journal, attempt and retained result."""
    prepared, accepted, inputs = {}, {}, []
    try:
        for plan, entries in reversed(history):
            for index, entry in enumerate(entries):
                if entry["kind"] == "DELIVERY_QUIESCED":
                    proof = entry["data"]
                    message_id = proof["message_id"]
                    if message_id in prepared:
                        raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")
                    _check_proof(window, plan, entries[:index], proof)
                    prepared[message_id] = (plan.sha256, proof)
                elif entry["kind"] == "OWNER_RECONCILED":
                    receipt = wire.validate_receipt(entry["data"])
                    proof = receipt["quiescence"]
                    message_id = proof["message_id"]
                    if (message_id in accepted or prepared.get(message_id) != (plan.sha256, proof)
                        or wire.timestamp(entry["at"]) < wire.timestamp(receipt["at"])):
                        raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")
                    accepted[message_id] = (receipt, wire.digest(entry))
                elif entry["kind"] == "OWNER_INPUT_RECONCILED":
                    inputs.append((plan.sha256, entry, set(accepted)))
        projected = {row["message_id"]: row for row in rows(window)}
        if set(projected) != set(prepared):
            raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")
        for message_id, (plan_hash, proof) in prepared.items():
            row = projected[message_id]
            receipt = json.loads(row["receipt_json"]) if row["receipt_json"] else None
            if (row["plan_hash"] != plan_hash or json.loads(row["proof_json"]) != proof
                or (receipt, row["receipt_record_sha256"]) != accepted.get(
                    message_id, (None, None))):
                raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")
        receipts = {key: value[0] for key, value in accepted.items()}
        seen = set()
        for plan_hash, entry, available in inputs:
            data = entry["data"]
            prior_receipts = {key: value for key, value in receipts.items() if key in available}
            receipt = next((value for value in prior_receipts.values()
                            if value["receipt_sha256"] == data.get("owner_receipt_sha256")), None)
            source = _closed_input(window, receipt, prior_receipts, history) if receipt else None
            if (source is None or data != {"event_hash": wire.digest(source["event_id"]),
                "input_sha256": source["input_hash"],
                "owner_receipt_sha256": receipt["receipt_sha256"],
                "core_record_sha256": receipt["record_sha256"]}
                or source["plan_hash"] != plan_hash or data["event_hash"] in seen
                or wire.timestamp(entry["at"]) < wire.timestamp(receipt["at"])):
                raise ValueError("DELIVERY_INPUT_CLOSURE_REQUIRED")
            seen.add(data["event_hash"])
    except (KeyError, TypeError, InvalidSignature) as error:
        raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED") from error


def _stopped(window):
    row = window.verify(active=False)
    records, head = window.read_journal()
    if (not window.spool.reconciliation_only or window.spool.lock is None or window.busy
        or row["state"] != "STOPPED" or window.plan.mode != "live_text"
        or window.stop_path.is_symlink() or not window.stop_path.is_file()
        or window.stop_path.read_bytes() != b"OWNER_STOP\n"
        or not any(entry["kind"] == "CORE_STOP_CONFIRMED" for entry in records)):
        raise ValueError("DELIVERY_QUIESCENT_PROCESS_REQUIRED")
    return records, head


async def prepare(window, client, message_id):
    entries, head = _stopped(window)
    stored = next((row for row in rows(window) if row["message_id"] == message_id), None)
    if stored:
        if stored["receipt_json"] is not None:
            return {"state": "OWNER_RESOLVED", "message_id": message_id, "paused": True}
        proof = json.loads(stored["proof_json"])
    else:
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(
            window.plan.config.capture_signing_key or ""))
        if key.public_key().public_bytes_raw().hex() != window.plan.value["live_text"][
            "capture_public_key"]:
            raise ValueError("DELIVERY_ORIGINAL_SIGNING_KEY_REQUIRED")
        stops = [entry for entry in entries if entry["kind"] == "STOP"]
        confirmations = [entry for entry in entries if entry["kind"] == "CORE_STOP_CONFIRMED"]
        if not stops or wire.timestamp(window.spool.quiesced_at) > window.clock():
            raise ValueError("DELIVERY_QUIESCENT_PROCESS_REQUIRED")
        proof = {"kind": wire.QUIESCENCE_KIND, "version": 1,
            **_source(window, window.plan, entries, message_id),
            "stop_record_sha256": wire.digest(stops[-1]),
            "core_stop_record_sha256": wire.digest(confirmations[-1]),
            "bridge_journal_head_sha256": head, "stopped_at": stops[-1]["at"],
            "quiesced_at": window.spool.quiesced_at}
        proof["signature"] = key.sign(wire.signing_payload(proof)).hex()
        _check_proof(window, window.plan, entries, proof)
        window.db.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (message_id TEXT PRIMARY KEY,"
            "plan_hash TEXT NOT NULL,proof_json TEXT NOT NULL,receipt_json TEXT,"
            "receipt_record_sha256 TEXT)")
        updated = window.append("DELIVERY_QUIESCED", proof)
        with window.db:
            window.db.execute(f"INSERT INTO {TABLE} VALUES (?,?,?,NULL,NULL)",
                              (message_id, window.plan.sha256, encode(proof)))
            window.db.execute("UPDATE test_window SET journal_head=?", (updated,))
    before = window.row()["journal_head"]
    result = await window.core_post(client, "/internal/v1/deliveries/reconciliation/prepare", proof)
    _stopped(window)
    expected = {"state": "READY", "message_id": message_id, "quiescence_sha256": wire.digest(proof)}
    if result != expected or window.row()["journal_head"] != before:
        raise ValueError("DELIVERY_PREPARATION_RECEIPT_INVALID")
    return result | {"paused": True}


async def reconcile(window, client, message_id):
    _, before = _stopped(window)
    stored = next((row for row in rows(window) if row["message_id"] == message_id), None)
    if not stored:
        raise ValueError("DELIVERY_QUIESCENCE_REQUIRED")
    proof = json.loads(stored["proof_json"])
    value = await window.core_post(client,
        f"/internal/v1/deliveries/{message_id}/reconciliation/lookup",
        {"proof_sha256": wire.digest(proof)})
    _stopped(window)
    if window.row()["journal_head"] != before:
        raise ValueError("DELIVERY_RESOLUTION_JOURNAL_REQUIRED")
    if value == {"state": "NOT_FOUND"} and stored["receipt_json"] is None:
        return {"state": "AWAITING_OWNER", "message_id": message_id, "paused": True}
    if (not isinstance(value, dict) or set(value) != {"state", "receipt"}
        or value["state"] != "FOUND"):
        raise ValueError("DELIVERY_RESOLUTION_RECEIPT_INVALID")
    receipt = wire.validate_receipt(value["receipt"])
    if (receipt["quiescence"] != proof or wire.timestamp(receipt["at"]) > window.clock()
        or stored["receipt_json"] is not None and json.loads(stored["receipt_json"]) != receipt):
        raise ValueError("DELIVERY_RESOLUTION_RECEIPT_INVALID")
    if stored["receipt_json"] is None:
        updated = window.append("OWNER_RECONCILED", receipt)
        with window.db:
            window.db.execute(f"UPDATE {TABLE} SET receipt_json=?,receipt_record_sha256=? "
                              "WHERE message_id=?", (encode(receipt), updated, message_id))
            window.db.execute("UPDATE test_window SET journal_head=?", (updated,))
    window.verify(active=False)
    history = window.verify_history(window.read_journal()[0])
    receipts = {row["message_id"]: json.loads(row["receipt_json"])
                for row in rows(window) if row["receipt_json"]}
    source = _closed_input(window, receipt, receipts, history)
    if (source is not None
        and wire.digest(source["event_id"]) not in terminal_inputs(window, history)):
        updated = window.append("OWNER_INPUT_RECONCILED", {
            "event_hash": wire.digest(source["event_id"]), "input_sha256": source["input_hash"],
            "owner_receipt_sha256": receipt["receipt_sha256"],
            "core_record_sha256": receipt["record_sha256"]})
        with window.db:
            window.db.execute("UPDATE test_window SET journal_head=?", (updated,))
        window.verify(active=False)
    return {"state": "OWNER_RESOLVED", "message_id": message_id,
            "outcome": receipt["outcome"], "receipt_sha256": receipt["receipt_sha256"],
            "paused": True}


async def command(config, args):
    from .network import private_http_client
    from .window import TestWindow, stored_plan

    if not wire.identifier(args.message_id, r"msg_[a-f0-9]{32}"):
        raise ValueError("DELIVERY_MESSAGE_REQUIRED")
    # The exclusive lock is acquired by a dedicated process that cannot receive/send.
    with Spool(config, reconciliation_only=True) as spool:
        if not spool.db.execute("SELECT 1 FROM sqlite_master WHERE name='test_window'").fetchone():
            raise ValueError("WINDOW_NOT_FOUND")
        saved = spool.db.execute("SELECT plan_json FROM test_window WHERE singleton=1").fetchone()
        if not saved:
            raise ValueError("WINDOW_NOT_FOUND")
        window = TestWindow(spool, stored_plan(saved[0], config))
        async with private_http_client(config.core_url, config.bridge_token) as client:
            operation = prepare if args.command == "window-delivery-prepare" else reconcile
            return await operation(window, client, args.message_id)
