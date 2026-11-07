"""Owner decisions close retained send responsibility in the original Budget Journal.

The Bridge proves that its old I/O process has stopped. This service records the
owner's separate decision, never a provider acknowledgement or a new send.
"""

import json
from contextlib import nullcontext
from types import SimpleNamespace

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import companion_delivery_protocol as wire
from companion import cumulative, paid_budget, transport
from companion.contracts import CoreError
from companion.privacy import reply_content_erased_by_memory
from companion.store import encode, utcnow

KIND = "quota_send_resolution"
PREPARED = "delivery_quiescence:"
PROJECTION = "delivery_resolution:"
ROW_FIELDS = {
    "kind", "version", "decision_source", "at", "journal_id", "account_ref", "policy_sha256",
    "key", "source_sha256", "owner_operation_id", "owner_operation_sha256", "outcome",
    "prior_liability_sha256", "prior_quota_state", "core_stop_sha256", "delivery", "quiescence",
    "input_closure", "previous",
}


def _meta(store, key):
    row = store.one("SELECT value FROM runtime_meta WHERE key=?", (key,))
    return json.loads(row["value"]) if row else None


def _put(store, key, value):
    store.execute("INSERT OR REPLACE INTO runtime_meta VALUES (?,?)", (key, encode(value)))


def _transaction(store):
    return nullcontext() if store.db.in_transaction else store.transaction()


def _row(store, message_id):
    row = store.one("SELECT o.*,c.scope,c.owner_actor_id FROM outbox o "
                    "JOIN conversations c USING(conversation_id) WHERE message_id=?", (message_id,))
    if (not row or row["action_type"] not in wire.ACTIONS or row["scope"] != "private"
        or row["owner_actor_id"] != "owner"
        or (row["action_type"] in wire.ACCOUNT_ACTIONS) != (row["delivery_lane"] == "qq_account")):
        raise CoreError("DELIVERY_RECONCILIATION_SOURCE_REQUIRED", 409)
    return row


def _authority(store, message_id):
    values = [_meta(store, prefix + message_id) for prefix in (
        "private_life_delivery:", "live_text_reply:", "account_action_delivery:", "owner_delivery:",
    )]
    present = [value for value in values if value is not None]
    if len(present) > 1:
        raise ValueError("ambiguous delivery authority")
    return present[0] if present else None


def _custody(store, message_id):
    projection = _meta(store, cumulative.PROJECTION)
    if projection:
        return SimpleNamespace(journal_path=projection["journal_path"])
    original = store.one("SELECT authority_json FROM outbox WHERE message_id=?", (message_id,))
    if original and original["authority_json"] != "{}":
        from companion_delivery_protocol.authority import AuthorityRef

        ref = AuthorityRef.from_dict(json.loads(original["authority_json"]))
        revision = store.one("SELECT * FROM owner_authority_revisions "
                             "WHERE grant_id=? AND revision=?", (ref.grant_id, ref.revision))
        from companion.owner_authority import reference

        if revision is None or reference(revision) != ref:
            raise ValueError("original owner delivery authority missing")
        return SimpleNamespace(journal_path=revision["journal_path"])
    authority = _authority(store, message_id)
    history = _meta(store, "live_text_window:" + authority["plan_sha256"]) if authority else None
    if history:
        return SimpleNamespace(journal_path=history["binding"]["budget_journal_path"])
    return None


def _frozen(store, proof):
    history = _meta(store, "live_text_window:" + proof["plan_sha256"])
    if not history or history.get("state") not in {"OPEN", "STOPPED", "RETIRED"}:
        raise ValueError("original approved window missing")
    approval, binding = history["approval"], history["binding"]
    if (approval.get("live_approved") is not True
        or binding["approval_sha256"] != wire.digest(approval)
        or approval["budget_journal_id"] != proof["budget_journal_id"]
        or approval["plan_sha256"] != proof["plan_sha256"]
        or approval["window_id"] != proof["window_id"]
        or binding["plan_sha256"] != proof["plan_sha256"]
        or binding["window_id"] != proof["window_id"]
        or binding["budget_journal_id"] != proof["budget_journal_id"]):
        raise ValueError("original approved window changed")
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(approval["capture_public_key"])).verify(
        bytes.fromhex(proof["signature"]), wire.signing_payload(proof),
    )
    return history


def _binding(store, proof, *, recorded=None):
    """Use retained authority hashes; erasure never requires reconstructing text."""
    wire.validate_quiescence(proof)
    if proof["version"] == 2:
        from companion import owner_delivery_reconciliation

        row = _row(store, proof["message_id"])
        return owner_delivery_reconciliation.binding(
            store, proof, row, _authority(store, row["message_id"]), recorded)
    history = _frozen(store, proof)
    row = _row(store, proof["message_id"])
    authority = _authority(store, row["message_id"])
    if proof["action_type"] in wire.ACCOUNT_ACTIONS:
        return _account_binding(store, proof, history, row, authority, recorded)
    if (not authority or any(authority.get(k) != proof[k] for k in (
        "message_id", "event_id", "plan_sha256"))
        or row["origin_event_id"] != proof["event_id"]
        or row["action_type"] != proof["action_type"] or row["attempt_count"] < 1):
        raise ValueError("original delivery authority changed")
    content_hash = authority.get("content_sha256") or row["content_sha256"]
    if not wire.hash_value(content_hash):
        # A completed resolution may retain the former content-object hash.
        content_hash = recorded["content_sha256"] if recorded else None
    if not wire.hash_value(content_hash) and not (
        content_hash is None and proof["payload_kind"] == "reply_sha256"
        and wire.hash_value(authority.get("reply_sha256"))
    ):
        raise ValueError("retained delivery content hash required")
    if (proof["content_sha256"] is not None and proof["content_sha256"] != content_hash
        or authority.get("action_type", row["action_type"]) != row["action_type"]
        or authority.get("delivery_key", row["delivery_key"]) != row["delivery_key"]
        or authority.get("reply_sha256", proof["payload_sha256"]) != proof["payload_sha256"]):
        raise ValueError("original delivery hashes changed")
    if row["content_sha256"]:
        content = json.loads(row["content_json"])
        if (row["content_sha256"] != content_hash or paid_budget.sha(content) != content_hash
            or paid_budget.sha(content if proof["payload_kind"] == "content_sha256"
                               else content["text"]) != proof["payload_sha256"]):
            raise ValueError("original delivery content changed")
    elif not (store.one("SELECT 1 FROM events e JOIN runtime_meta r "
                       "ON r.key='erased_event:'||e.event_id WHERE e.event_id=? AND e.redacted=1",
                       (row["origin_event_id"],))
              or reply_content_erased_by_memory(store, row)):
        raise ValueError("unproved erased delivery")
    _voice_binding(store, proof, history, row, content_hash)
    token = row["lease_token"]
    if token:
        check = {"window_sha256": proof["plan_sha256"], "message_id": proof["message_id"],
                 "event_id": proof["event_id"], proof["payload_kind"]: proof["payload_sha256"],
                 "lease_token": token}
        if proof["reply_revision"] is not None:
            check["reply_revision"] = proof["reply_revision"]
        if "voice_check" in proof:
            check["voice"] = proof["voice_check"]
        if (wire.digest(token) != proof["lease_sha256"]
            or wire.digest(check) != proof["send_check_sha256"]):
            raise ValueError("original send fence changed")
    elif recorded is None:
        raise ValueError("original send lease missing")
    delivery = {"message_id": row["message_id"], "event_id": row["origin_event_id"],
        "action_type": row["action_type"], "content_sha256": content_hash,
        "delivery_key_sha256": wire.digest(row["delivery_key"]),
        "authority_sha256": wire.digest(authority), "attempt_count": row["attempt_count"],
        "created_at": row["created_at"], "lease_until": row["lease_until"],
        "original_state": recorded["original_state"] if recorded else row["state"]}
    wire.validate_delivery(delivery)
    if recorded is not None and recorded != delivery:
        raise ValueError("retained delivery binding changed")
    return row, history, delivery


def _voice_binding(store, proof, history, row, content_hash):
    from companion.voice_original_proof import validate_original_voice_binding

    given = proof.get("voice_check")
    value = _meta(store, "voice_reply:" + row["message_id"])
    job = (store.one("SELECT * FROM voice_jobs WHERE job_id=?", (given["job_id"],))
           if given else None)
    approval = history["approval"]
    if given and approval.get("voice_scope") != "owner_voice_v1":
        raise ValueError("original voice scope missing")
    validate_original_voice_binding(receipt=value, job=job, proof=proof, outbox=row,
        content_sha256=content_hash, original_scope={"window_sha256": proof["plan_sha256"]},
        voice_policy_sha256=approval.get("voice_policy_sha256"))

def _account_binding(store, proof, history, row, authority, recorded):
    reservation = store.one("SELECT * FROM social_autonomy_reservations WHERE message_id=?",
                            (row["message_id"],))
    plan = store.one("SELECT * FROM social_autonomy_plans WHERE plan_id=?",
                    (proof["account_plan_id"],))
    approval = history["approval"]
    if (not reservation or not plan or not authority
        or reservation["plan_id"] != proof["account_plan_id"]
        or reservation["action_type"] != proof["action_type"]
        or reservation["released_at"] is not None or reservation["dispatched_at"] is None
        or row["action_type"] != proof["action_type"] or row["attempt_count"] < 1
        or row["provider_message_id"] is not None or row["native_ack_json"] is not None
        or approval.get("account_actions_scope") != "qq_social_v1"
        or approval.get("account_actions_authorization_sha256") != proof["authorization_sha256"]
        or any(authority.get(k) != proof[k] for k in (
            "message_id", "plan_sha256", "content_sha256", "payload_sha256",
            "authorization_sha256"))
        or any(plan[k] != proof[k] for k in ("payload_sha256", "authorization_sha256"))):
        raise ValueError("original account authority changed")
    native_receipt = json.loads(reservation["receipt_json"])
    if (reservation["receipt_sha256"] != (wire.digest(native_receipt) if native_receipt else None)
        or native_receipt and (native_receipt.get("result") != "UNKNOWN"
            or any(native_receipt.get(k) != proof[k] for k in (
                "message_id", "action_type", "payload_sha256")))):
        raise ValueError("original account receipt changed")
    if row["content_sha256"]:
        content = json.loads(row["content_json"])
        if (row["content_sha256"] != proof["content_sha256"]
            or wire.digest(content) != proof["content_sha256"]
            or content != json.loads(plan["plan_json"])
            or wire.digest({k: v for k, v in content.items() if k != "payload_sha256"})
                != proof["payload_sha256"]
            or any(content.get(k) != v for k, v in {
                "plan_id": proof["account_plan_id"], "message_id": row["message_id"],
                "action_type": row["action_type"], "delivery_key": row["delivery_key"],
                "policy_sha256": plan["policy_sha256"],
                "source_snapshot_sha256": plan["source_snapshot_sha256"],
                "authorization_sha256": proof["authorization_sha256"],
            }.items())):
            raise ValueError("original account payload changed")
    elif not (plan["redacted"] and plan["state"] == "CANCELLED"
              and plan["last_error_code"] == "SOCIAL_SOURCE_ERASED"
              and plan["plan_json"] == plan["input_snapshot_json"] == "{}"):
        raise ValueError("unproved erased account delivery")
    token = row["lease_token"]
    if token:
        check = {"window_sha256": proof["plan_sha256"], "message_id": row["message_id"],
                 "lease_token": token, "content_sha256": proof["content_sha256"],
                 "payload_sha256": proof["payload_sha256"]}
        if (wire.digest(token) != proof["lease_sha256"]
            or wire.digest(check) != proof["send_check_sha256"]
            or native_receipt and native_receipt.get("lease_token") != token):
            raise ValueError("original account send fence changed")
    elif recorded is None:
        raise ValueError("original account send lease missing")
    delivery = {"message_id": row["message_id"], "event_id": row["origin_event_id"],
        "action_type": row["action_type"], "content_sha256": proof["content_sha256"],
        "delivery_key_sha256": wire.digest(row["delivery_key"]),
        "authority_sha256": wire.digest(authority), "attempt_count": row["attempt_count"],
        "created_at": row["created_at"], "lease_until": row["lease_until"],
        "original_state": recorded["original_state"] if recorded else row["state"],
        "account_plan_id": proof["account_plan_id"],
        "authorization_sha256": proof["authorization_sha256"],
        "payload_sha256": proof["payload_sha256"],
        "reservation_sha256": wire.digest({k: v for k, v in reservation.items()
                                           if k not in {"receipt_json", "receipt_sha256"}}),
        "account_receipt_sha256": reservation["receipt_sha256"]}
    wire.validate_delivery(delivery)
    if recorded is not None and recorded != delivery:
        raise ValueError("retained account binding changed")
    return row, history, delivery


def receipt(row, record_sha256):
    result = {"kind": wire.RECEIPT_KIND, "version": 1, "state": "OWNER_RESOLVED",
        "paused": True, **{key: row[key] for key in wire.RECEIPT_FIELDS - {
            "kind", "version", "state", "paused", "record_sha256", "receipt_sha256"}},
        "record_sha256": record_sha256}
    result["receipt_sha256"] = wire.digest(result)
    return wire.validate_receipt(result)


def _shape(row):
    if (not isinstance(row, dict) or set(row) != ROW_FIELDS or row["kind"] != KIND
        or type(row["version"]) is not int or row["version"] != 1
        or row["decision_source"] != "owner_manual"
        or row["key"] != "send:" + wire.digest(row["delivery"]["message_id"])
        or not wire.hash_value(row["previous"])):
        raise ValueError("invalid owner send resolution record")
    receipt(row, wire.digest(row))


def _provider_receipt(row):
    return wire.digest({"provider_message_id": row["provider_message_id"],
                        "native_ack": json.loads(row["native_ack_json"])
                        if row["native_ack_json"] else None})


def _input_hash(store, event, proof, account):
    if event["actor_id"] != "owner" or event["privacy_scope"] != "private":
        return None
    if proof["version"] == 2:
        if event["event_type"] == "notice.poke":
            original = _meta(store, "poke_input:" + event["event_id"])
            if (not original or original.get("authority") != proof["authority"]
                or original.get("source_sha256") != event["fingerprint"]):
                return None
            admitted = account["operations"].get("input:" + wire.digest(event["event_id"]))
            if account["policy"] and (not admitted or admitted["state"] != "ADMITTED"
                or admitted.get("source_sha256") != event["fingerprint"]):
                return None
            return event["fingerprint"]
        original = _meta(store, "owner_input:" + event["event_id"])
        if (not original or original.get("authority") != proof["authority"]
            or original.get("event_id") != event["event_id"]
            or not wire.hash_value(original.get("input_sha256"))):
            return None
        admitted = account["operations"].get("input:" + wire.digest(event["event_id"]))
        if account["policy"] and (not admitted or admitted["state"] != "ADMITTED"
            or admitted.get("source_sha256") != wire.digest(original.get("capture"))):
            return None
        return original["input_sha256"]
    original = _meta(store, "live_text_input:" + event["event_id"])
    poke = _meta(store, "poke_input:" + event["event_id"])
    if (original and original.get("window_sha256") == proof["plan_sha256"]
        and original.get("event_id") == event["event_id"]
        and wire.hash_value(original.get("input_sha256"))):
        value, source = original["input_sha256"], wire.digest(original)
    elif (poke and poke.get("window_sha256") == proof["plan_sha256"]
          and event["event_type"] == "notice.poke"):
        value = source = event["fingerprint"]
    else:
        return None
    if account["policy"]:
        admitted = account["operations"].get("input:" + wire.digest(event["event_id"]))
        if (not admitted or admitted["state"] != "ADMITTED"
            or admitted["window_id"] != proof["window_id"]
            or admitted.get("source_sha256") != source):
            return None
    return value


def _input_closure(store, proof, delivery, outcome, account, *, global_models=True):
    if proof["action_type"] in wire.ACCOUNT_ACTIONS:
        return None
    """Freeze one bounded native snapshot; the current decision has no circular hash."""
    event_id, message_id = proof["event_id"], proof["message_id"]
    event = store.one("SELECT * FROM events WHERE event_id=?", (event_id,))
    if not event or not wire.hash_value(event["fingerprint"]):
        raise ValueError("original input fingerprint required")
    maximum = wire.MAX_INPUT_DELIVERIES
    jobs = store.rows("SELECT j.job_id,j.event_id,j.kind,j.state,j.context_tracked,j.created_at,"
        "j.updated_at,j.last_error_code FROM jobs j WHERE j.event_id=? OR EXISTS "
        "(SELECT 1 FROM job_sources s WHERE s.job_id=j.job_id AND s.event_id=?) "
        "ORDER BY j.job_id LIMIT ?", (event_id, event_id, maximum + 1))
    outbox = store.rows("SELECT * FROM outbox WHERE origin_event_id=? ORDER BY message_id LIMIT ?",
                        (event_id, maximum + 1))
    complete = len(jobs) <= maximum and len(outbox) <= maximum
    jobs = jobs[:maximum]
    # An overlarge inventory remains explicitly incomplete, but includes this decision.
    if len(outbox) > maximum:
        current = next((r for r in outbox if r["message_id"] == message_id), None)
        current = current or _row(store, message_id)
        outbox = sorted([r for r in outbox if r["message_id"] != message_id][:maximum - 1]
                        + [current], key=lambda r: r["message_id"])
    deliveries, requests = [], set()
    for item in outbox:
        authority = _authority(store, item["message_id"])
        previous = account["send_resolutions"].get(item["message_id"])
        if authority:
            requests.update(authority.get("request_ids", []))
            if authority.get("request_id"):
                requests.add(authority["request_id"])
        content_hash = ((authority or {}).get("content_sha256") or item["content_sha256"]
                        or (previous or {}).get("delivery", {}).get("content_sha256"))
        entry = {"message_id": item["message_id"],
            "state": outcome if item["message_id"] == message_id else item["state"],
            "attempt_count": item["attempt_count"],
            "content_sha256": content_hash if wire.hash_value(content_hash) else None,
            "authority_sha256": wire.digest(authority) if authority else None,
            "owner_resolution_sha256": wire.digest(previous) if previous
                and item["message_id"] != message_id else None,
            "provider_receipt_sha256": _provider_receipt(item)}
        if item["message_id"] == message_id:
            entry.update({key: delivery[key] for key in (
                "content_sha256", "authority_sha256", "attempt_count",
            )})
        deliveries.append(entry)
    if len(requests) > maximum:
        complete = False
    request_ids = sorted(requests)[:maximum]
    placeholders = ",".join("?" for _ in request_ids) or "NULL"
    models = store.rows(
        "SELECT request_id,job_id,state,billed_micro_usd,input_tokens,output_tokens "
        f"FROM model_usage WHERE request_id IN ({placeholders}) OR job_id IN "
        "(SELECT j.job_id FROM jobs j WHERE j.event_id=? OR EXISTS "
        "(SELECT 1 FROM job_sources s WHERE s.job_id=j.job_id AND s.event_id=?)) "
        "ORDER BY request_id LIMIT ?", (*request_ids, event_id, event_id, maximum + 1))
    global_unsettled = store.rows("SELECT request_id,state FROM model_usage WHERE state!='SETTLED' "
        "ORDER BY request_id LIMIT ?", (maximum + 1,)) if global_models else []
    reads = store.rows("SELECT item_id AS id,state,read_request_id,usage_request_id "
        "FROM content_items WHERE owner_event_id=? ORDER BY item_id LIMIT ?",
        (event_id, maximum + 1))
    reads.extend(store.rows("SELECT request_id AS id,state FROM exploration_requests WHERE "
        "request_id IN (SELECT read_request_id FROM content_items WHERE owner_event_id=?) "
        "ORDER BY request_id LIMIT ?", (event_id, maximum + 1)))
    for table, identity, owner, states in (
        ("voice_jobs", "job_id", "event_id", {"QUEUED", "LEASED", "RUNNING", "UNKNOWN"}),
        ("bili_video_items", "item_id", "owner_event_id",
         {"DECLARED", "PREPARED", "ANALYZED", "UNKNOWN"}),
    ):
        if store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)):
            cleanup = ",cleanup_state" if table == "voice_jobs" else ""
            extra = store.rows(f"SELECT {identity} AS id,state{cleanup} FROM {table} "
                               f"WHERE {owner}=? ORDER BY {identity} LIMIT ?",
                               (event_id, maximum + 1))
            reads.extend({**item, "active": item["state"] in states
                          or item.get("cleanup_state") == "PENDING"} for item in extra)
    complete &= all(len(items) <= maximum for items in (models, global_unsettled, reads))
    primary = [job for job in jobs if job["kind"] == "chat" and job["event_id"] == event_id]
    if not primary:
        primary = [job for job in jobs if job["kind"] == "poke"]
    job = primary[0] if len(primary) == 1 else None
    delegated = {item["id"] for item in reads if "read_request_id" not in item}
    unsettled = {
        "jobs": any(item["state"] not in wire.JOB_TERMINAL for item in jobs),
        "models": bool(global_unsettled) or any(item["state"] != "SETTLED" for item in models),
        # A native agent read's link row stays WAITING; its exploration request row
        # (also in `reads`) carries the actual state.
        "reads": any(item.get("active", item["state"] in {
            "WAITING", "QUEUED", "FETCHING", "RUNNING", "LEASED", "UNKNOWN"})
            and not (item["state"] == "WAITING" and item.get("read_request_id") in delegated)
            for item in reads),
        "deliveries": any(item["state"] not in wire.DELIVERY_TERMINAL for item in deliveries),
    }
    input_hash = _input_hash(store, event, proof, account)
    result = {"version": 1, "event_id": event_id, "source_sha256": event["fingerprint"],
        "input_sha256": input_hash,
        "job": ({key: job[key] for key in ("job_id", "kind", "state")}
                | {"record_sha256": wire.digest(job)}) if job else None,
        "work_sha256": wire.digest({"jobs": jobs, "models": models[:maximum],
            "global_unsettled": global_unsettled[:maximum], "reads": reads[:maximum]}),
        "complete": bool(complete), "unsettled": unsettled, "deliveries": deliveries,
        "terminal": bool(complete and input_hash and job and job["state"] in wire.JOB_TERMINAL
            and not any(unsettled.values()) and all(item["authority_sha256"]
                or not item["attempt_count"] for item in deliveries))}
    return wire.validate_input_closure(result)


def _closure_references(state, row):
    if row["input_closure"] is None:
        return
    for item in row["input_closure"]["deliveries"]:
        previous = state["send_resolutions"].get(item["message_id"])
        if item["owner_resolution_sha256"] is None:
            if previous:
                raise ValueError("input closure omitted a preceding owner decision")
            continue
        if (not previous or wire.digest(previous) != item["owner_resolution_sha256"]
            or previous["delivery"]["event_id"] != row["delivery"]["event_id"]
            or previous["outcome"] != item["state"]
            or any(previous["delivery"][key] != item[key] for key in (
                "attempt_count", "content_sha256", "authority_sha256",
            )) or next(entry for entry in previous["input_closure"]["deliveries"]
                       if entry["message_id"] == item["message_id"])["provider_receipt_sha256"]
                != item["provider_receipt_sha256"]):
            raise ValueError("input closure must reference a preceding owner decision")


def _repair_input_closure(store, view, row):
    old = row["input_closure"]
    if old is None or not old["terminal"]:
        return
    current = _input_closure(store, row["quiescence"], row["delivery"], row["outcome"],
                             view["cumulative"], global_models=False)
    if (not current["terminal"] or any(current[key] != old[key] for key in (
        "event_id", "source_sha256", "input_sha256", "deliveries",
    )) or not current["job"] or any(current["job"][key] != old["job"][key]
                                     for key in ("job_id", "kind", "state"))):
        raise CoreError("DELIVERY_INPUT_CLOSURE_RECONCILIATION_REQUIRED", 409)


def fold_record(state, row):
    """Pure fold: terminal owner responsibility retains every original dimension."""
    _shape(row)
    _closure_references(state, row)
    message_id = row["delivery"]["message_id"]
    prior = state["operations"].get(row["key"])
    if (message_id in state["send_resolutions"]
        or any(value["owner_operation_id"] == row["owner_operation_id"]
               for value in state["send_resolutions"].values())):
        raise ValueError("owner resolution replay")
    if state["policy"]:
        if (row["account_ref"] != state["policy"]["account_ref"]
            or row["policy_sha256"] != state["policy_sha256"] or not prior
            or prior["state"] not in {"RESERVED", "UNKNOWN", "DELIVERY_UNCERTAIN"}
            or row["prior_quota_state"] != prior["state"]
            or row["prior_liability_sha256"] != wire.digest(prior)
            or prior["source_sha256"] != row["source_sha256"]
            or row["quiescence"]["version"] == 1
            and prior["window_id"] != row["quiescence"]["window_id"]
            or prior["logical_request_id"] != _logical_request(row["quiescence"])
            or cumulative.instant(row["at"]) != row["at"]
            or state["clock"] and row["at"] < state["clock"]):
            raise ValueError("unbound owner send resolution")
        prior["state"] = "OWNER_SENT" if row["outcome"] == "SENT" else "OWNER_CLOSED"
        state.update(paused=True, clock=row["at"],
                     reason=state["reason"] or "OWNER_DELIVERY_RECONCILIATION")
    elif any(row[key] is not None for key in (
        "account_ref", "policy_sha256", "prior_liability_sha256", "prior_quota_state",
    )) or prior:
        raise ValueError("unbound non-cumulative owner resolution")
    state["send_resolutions"][message_id] = dict(row)


def validate_journal_record(store, row, record_sha256, lifecycle, journal_id, authorities=()):
    _shape(row)
    proof = row["quiescence"]
    if proof["version"] == 2:
        from companion import owner_delivery_reconciliation

        if (row["journal_id"] != journal_id
            or row["core_stop_sha256"] != proof["core_stop_record_sha256"]):
            raise ValueError("original native journal required")
        owner_delivery_reconciliation.stopped(
            store, proof, authorities[-1] if authorities else None)
    elif (row["journal_id"] != journal_id or not lifecycle or lifecycle["state"] != "STOPPED"
        or lifecycle["plan_sha256"] != proof["plan_sha256"]
        or lifecycle["window_id"] != proof["window_id"]
        or lifecycle["head_sha256"] != row["core_stop_sha256"]):
        raise ValueError("original stopped execution required")
    try:
        original, _, _ = _binding(store, proof, recorded=row["delivery"])
        if row["input_closure"] is not None:
            current = next(item for item in row["input_closure"]["deliveries"]
                           if item["message_id"] == proof["message_id"])
            event = store.one("SELECT fingerprint FROM events WHERE event_id=?",
                              (proof["event_id"],))
            if (not event or event["fingerprint"] != row["input_closure"]["source_sha256"]
                or current["provider_receipt_sha256"] != _provider_receipt(original)):
                raise ValueError("retained input or provider receipt changed")
    except InvalidSignature as error:
        raise ValueError("owner resolution quiescence signature changed") from error
    receipt(row, record_sha256)


def replay_matches(row, selector):
    return bool(selector and set(selector) == {
        "message_id", "operation_id", "operation_sha256", "outcome",
    } and row["delivery"]["message_id"] == selector["message_id"]
        and row["owner_operation_id"] == selector["operation_id"]
        and row["owner_operation_sha256"] == selector["operation_sha256"]
        and row["outcome"] == selector["outcome"])


def validate_projection(store, resolutions, selector=None):
    projected = {row["key"].removeprefix(PROJECTION): json.loads(row["value"])
                 for row in store.rows("SELECT key,value FROM runtime_meta WHERE substr(key,1,?)=?",
                                       (len(PROJECTION), PROJECTION))}
    if set(projected) - set(resolutions):
        raise ValueError("owner resolution journal behind database")
    for message_id, row in resolutions.items():
        current = _row(store, message_id)
        if replay_matches(row, selector):
            if (projected.get(message_id) not in {None, row["sha256"]}
                or current["state"] not in {
                    row["delivery"]["original_state"], "DELIVERY_UNCERTAIN", row["outcome"],
                }):
                raise ValueError("owner resolution recovery conflict")
            continue
        if (projected.get(message_id) != row["sha256"] or current["state"] != row["outcome"]
            or current["lease_token"] is not None):
            raise ValueError("owner resolution database behind journal")


def _ready(store, custody, proof, now):
    view = paid_budget.journal(store, custody, allow_settlement=True, initialize=False)
    row, history, delivery = _binding(store, proof)
    lifecycle, account = view["lifecycle"], view["cumulative"]
    native = proof["version"] == 2
    if native:
        from companion import owner_delivery_reconciliation

        owner_delivery_reconciliation.stopped(store, proof, view["owner_authority"], current=True)
    if (not native and (not lifecycle or lifecycle["state"] != "STOPPED"
        or lifecycle["plan_sha256"] != proof["plan_sha256"]
        or lifecycle["window_id"] != proof["window_id"] or history["state"] != "STOPPED"
        or not store.one("SELECT 1 FROM runtime_meta WHERE key=? AND value='OWNER_STOP'",
                         ("live_text_stopped:" + proof["plan_sha256"],)))
        or wire.timestamp(proof["quiesced_at"]) > wire.timestamp(now)
        or row["state"] not in {"SENDING", "DELIVERY_UNCERTAIN"}
        or row["state"] == "SENDING" and (
            row["lease_until"] is None
            or wire.timestamp(row["lease_until"]) > wire.timestamp(now))):
        raise CoreError("DELIVERY_RECONCILIATION_STOP_REQUIRED", 409)
    prior = account["operations"].get("send:" + wire.digest(proof["message_id"]))
    frozen_policy = None if native else history["approval"].get("cumulative")
    if account["policy"]:
        if (not native and (not frozen_policy
                            or wire.digest(frozen_policy) != account["policy_sha256"])
            or not prior or prior["state"] not in {"RESERVED", "UNKNOWN", "DELIVERY_UNCERTAIN"}
            or prior["source_sha256"] != proof["send_check_sha256"]
            or not native and prior["window_id"] != proof["window_id"]
            or prior["logical_request_id"] != _logical_request(proof)):
            raise CoreError("DELIVERY_RECONCILIATION_ACCOUNT_CONFLICT", 409)
    elif frozen_policy or prior:
        raise CoreError("DELIVERY_RECONCILIATION_ACCOUNT_CONFLICT", 409)
    return view, delivery, prior


def _logical_request(proof):
    return proof["account_plan_id"] if proof["action_type"] in wire.ACCOUNT_ACTIONS else (
        proof["event_id"])


def prepare(store, settings, proof, now):
    """Bridge-authenticated preparation; this never chooses an owner outcome."""
    try:
        with _transaction(store):
            wire.validate_quiescence(proof)
            custody = _custody(store, proof["message_id"])
            if custody is None:
                raise CoreError("DELIVERY_RECONCILIATION_SOURCE_REQUIRED", 409)
            from companion import owner_delivery_no_io

            if owner_delivery_no_io.candidate(store, proof):
                result = owner_delivery_no_io.prepare(store, custody, proof, now)
                return {"state": "NOT_SENT", "message_id": proof["message_id"], "receipt": result}
            _ready(store, custody, proof, now)
            existing = _meta(store, PREPARED + proof["message_id"])
            if existing is not None and existing != proof:
                raise CoreError("DELIVERY_RECONCILIATION_CONFLICT", 409)
            _put(store, PREPARED + proof["message_id"], proof)
            return {"state": "READY", "message_id": proof["message_id"],
                    "quiescence_sha256": wire.digest(proof)}
    except (ValueError, TypeError, KeyError, InvalidSignature) as error:
        raise CoreError("DELIVERY_QUIESCENCE_INVALID", 409) from error


def _project(store, custody, view, row, now):
    message_id, outcome = row["delivery"]["message_id"], row["outcome"]
    current = _row(store, message_id)
    if current["state"] not in {
        row["delivery"]["original_state"], "DELIVERY_UNCERTAIN", outcome,
    }:
        raise CoreError("DELIVERY_RECONCILIATION_CONFLICT", 409)
    if (_meta(store, PROJECTION + message_id) != row["sha256"]
        or current["state"] != outcome or current["lease_token"] is not None):
        _repair_input_closure(store, view, row)
    store.retain_budget_stop(row["journal_id"], row["sha256"])
    transport.record_reconciled_outcome(store, message_id, outcome, row["at"])
    _put(store, PROJECTION + message_id, row["sha256"])
    if view["cumulative"]["policy"]:
        _put(store, cumulative.PROJECTION,
             cumulative.binding(view["cumulative"], custody.journal_path, view["head_sha256"]))
    if not store.one("SELECT 1 FROM audit_log WHERE action='delivery.owner_reconciled' "
                     "AND entity_id=?", (message_id,)):
        store.audit("delivery.owner_reconciled", message_id, "owner", now, outcome=outcome,
                    owner_operation_id=row["owner_operation_id"], record_sha256=row["sha256"])
    from companion.goals import acknowledge_commitment_delivery

    acknowledge_commitment_delivery(store, message_id, outcome, row["at"])


def lookup(store, settings, message_id, *, operation_id=None, operation_sha256=None,
           outcome=None, proof_sha256=None, repair=False, now=None):
    """Find the original durable record; exact owner retries may repair its projection."""
    try:
        with _transaction(store):
            custody = _custody(store, message_id)
            if custody is None:
                return None
            selector = None
            if operation_id is not None or operation_sha256 is not None or outcome is not None:
                wire.owner_operation(operation_id, operation_sha256)
                if outcome not in {"SENT", "CANCELLED"}:
                    raise ValueError("owner outcome required")
                selector = {"message_id": message_id, "operation_id": operation_id,
                            "operation_sha256": operation_sha256, "outcome": outcome}
            if repair and selector is None:
                raise CoreError("DELIVERY_OWNER_OPERATION_REQUIRED", 403)
            if proof_sha256 is not None and not wire.hash_value(proof_sha256):
                raise ValueError("proof hash required")
            view = paid_budget.journal(store, custody, allow_settlement=True, initialize=False,
                                       resolution_replay=selector if repair else None)
            row = view["send_resolutions"].get(message_id)
            if row is None:
                return None
            if ((selector and not replay_matches(row, selector))
                or proof_sha256 is not None and wire.digest(row["quiescence"]) != proof_sha256):
                raise CoreError("DELIVERY_RECONCILIATION_CONFLICT", 409)
            if repair:
                _project(store, custody, view, row, now or row["at"])
                paid_budget.journal(store, custody, allow_settlement=True, initialize=False)
            return receipt(row, row["sha256"])
    except (ValueError, TypeError, KeyError, InvalidSignature) as error:
        raise CoreError("DELIVERY_RECONCILIATION_CONFLICT", 409) from error


def _ordinary(store, message_id, outcome, now):
    row = _row(store, message_id)
    if row["action_type"] not in wire.PRIVATE_ACTIONS:
        raise CoreError("DELIVERY_QUIESCENCE_REQUIRED", 409)
    if row["state"] != "DELIVERY_UNCERTAIN":
        raise CoreError("NOT_UNCERTAIN", 409)
    transport.record_reconciled_outcome(store, message_id, outcome, now)
    store.audit("delivery.owner_reconciled", message_id, "owner", now, outcome=outcome)
    from companion.goals import acknowledge_commitment_delivery

    acknowledge_commitment_delivery(store, message_id, outcome, now)
    return {"state": outcome}


def resolve(store, settings, message_id, outcome, now, *, operation_id, operation_sha256):
    try:
        wire.owner_operation(operation_id, operation_sha256)
        if outcome not in {"SENT", "CANCELLED"}:
            raise CoreError("INVALID_RECONCILIATION", 400)
        with _transaction(store):
            existing = lookup(store, settings, message_id, operation_id=operation_id,
                operation_sha256=operation_sha256, outcome=outcome, repair=True, now=now)
            if existing:
                return {"state": existing["outcome"], "receipt": existing}
            custody = _custody(store, message_id)
            if custody is None:
                if (settings.mode != "live" and settings.live_text_window is None
                    and _meta(store, "live_text_window") is None):
                    return _ordinary(store, message_id, outcome, now)
                raise CoreError("DELIVERY_QUIESCENCE_REQUIRED", 409)
            proof = _meta(store, PREPARED + message_id)
            if proof is None:
                raise CoreError("DELIVERY_QUIESCENCE_REQUIRED", 409)
            view, delivery, prior = _ready(store, custody, proof, now)
            account = view["cumulative"]
            row = {"kind": KIND, "version": 1, "decision_source": "owner_manual",
                "at": max(cumulative.instant(now), account["clock"] or cumulative.instant(now)),
                "journal_id": proof["budget_journal_id"],
                "account_ref": account["policy"]["account_ref"] if account["policy"] else None,
                "policy_sha256": account["policy_sha256"],
                "key": "send:" + wire.digest(message_id),
                "source_sha256": proof["send_check_sha256"],
                "owner_operation_id": operation_id, "owner_operation_sha256": operation_sha256,
                "outcome": outcome, "prior_liability_sha256": wire.digest(prior) if prior else None,
                "prior_quota_state": prior["state"] if prior else None,
                "core_stop_sha256": (view["owner_authority"]["record_sha256"]
                    if proof["version"] == 2 else view["lifecycle"]["head_sha256"]),
                "delivery": delivery, "quiescence": proof,
                "input_closure": _input_closure(store, proof, delivery, outcome, account)}
            view = paid_budget.journal(store, custody, row, allow_settlement=True, initialize=False)
            stored = view["send_resolutions"][message_id]
            _project(store, custody, view, stored, now)
            paid_budget.journal(store, custody, allow_settlement=True, initialize=False)
            return {"state": outcome, "receipt": receipt(stored, stored["sha256"])}
    except (ValueError, TypeError, KeyError, InvalidSignature) as error:
        raise CoreError("DELIVERY_RECONCILIATION_CONFLICT", 409) from error


def readiness(store, settings, message_id, *, now=None):
    """Read one consistent snapshot; never append a record or repair a projection."""
    try:
        with _transaction(store):
            custody = _custody(store, message_id)
            if custody is None:
                ordinary = (settings.mode != "live" and settings.live_text_window is None
                            and _meta(store, "live_text_window") is None
                            and _row(store, message_id)["action_type"] in wire.PRIVATE_ACTIONS)
                return {"prepared": ordinary,
                        "reason": None if ordinary else "DELIVERY_QUIESCENCE_REQUIRED"}
            proof = _meta(store, PREPARED + message_id)
            if proof is None:
                return {"prepared": False, "reason": "DELIVERY_QUIESCENCE_REQUIRED"}
            # Use the caller's clock when supplied, including synthetic/rollback checks.
            _ready(store, custody, proof, now or utcnow())
            return {"prepared": True, "reason": None}
    except (CoreError, ValueError, TypeError, KeyError, InvalidSignature) as error:
        return {"prepared": False,
                "reason": getattr(error, "code", "DELIVERY_RECONCILIATION_CONFLICT")}


def is_resolved_account(store, message_id):
    """Recognize original owner responsibility without releasing historical quota."""
    row = _row(store, message_id)
    if row["action_type"] not in wire.ACCOUNT_ACTIONS or not _meta(store, PROJECTION + message_id):
        return False
    result = lookup(store, None, message_id)
    return bool(result and result["delivery"]["action_type"] in wire.ACCOUNT_ACTIONS)
