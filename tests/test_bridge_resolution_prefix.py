"""Historical native proofs keep their original prefix while replay stays linear."""

import copy
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import companion_delivery_protocol as wire
from companion_onebot_bridge import owner_native_resolution as resolution
from companion_onebot_bridge.spool import encode
from tests.support.bridge_owner_runtime import config as config


@pytest.fixture
def custody(config):
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE results(message_id TEXT PRIMARY KEY,body TEXT)")
        db.execute(f"CREATE TABLE {resolution.TABLE}(message_id TEXT PRIMARY KEY,"
            "plan_hash TEXT,proof_json TEXT,receipt_json TEXT,receipt_record_sha256 TEXT)")
        runtime = SimpleNamespace(db=db, config=config, authority=config.owner_authority,
                                  legacy=None)
        records = []
        append(records, "OWNER_BOUND", {"authority": runtime.authority})
        yield runtime, records


def append(records, kind, data):
    record = {"seq": len(records) + 1,
        "previous": wire.digest(records[-1]) if records else "0" * 64,
        "at": (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=len(records))).isoformat(),
        "kind": kind, "data": data}
    records.append(record)
    return record


def attempt(runtime, records, number, *, authority=None):
    authority = authority or runtime.authority
    message_id, event_id = f"msg_{number:032x}", f"event_{number:064x}"
    token = f"lease_{number:032x}"
    binding = {"action_type": "qq.private.reply", "content_sha256": wire.digest("content"),
        "payload_kind": "reply_sha256", "payload_sha256": wire.digest("text"),
        "lease_sha256": wire.digest(token), "reply_revision": 1,
        "send_check_sha256": wire.digest({"authority": authority, "event_id": event_id,
            "message_id": message_id, "lease_token": token,
            "content_sha256": wire.digest("content"), "reply_revision": 1})}
    reserved = append(records, "RESERVED", {"message_id": message_id, "event_id": event_id,
        "fence": number, "text_hash": binding["content_sha256"], "item_hash": wire.digest(number),
        "delivery_binding": binding})
    started = append(records, "IO_STARTED", {"message_id": message_id, "provider_ack_hash": None})
    body = {"result": "DELIVERY_UNCERTAIN", "lease_token": token}
    runtime.db.execute("INSERT INTO results VALUES (?,?)", (message_id, encode(body)))
    return SimpleNamespace(reserved=reserved, last=started, body=body, authority=authority)


def stop(records, authority):
    stopped = append(records, "OWNER_STOPPED", {"authority": authority})
    confirmed = append(records, "CORE_STOP_CONFIRMED", {"authority": authority,
        "state": "STOPPED",
        "stopped_authority": authority | {"revision": authority["revision"] + 1},
        "core_stop_sha256": wire.digest(stopped), "budget_journal_id": "budget_" + "a" * 32,
        "quiescence_proven": False})
    return stopped, confirmed


def prepare(runtime, records, original, stopped, confirmed):
    value = original.reserved["data"]
    proof = {"kind": "owner_delivery_quiescence_v2", "version": 2,
        "authority": original.authority, "message_id": value["message_id"],
        "event_id": value["event_id"], "fence": value["fence"], "item_sha256": value["item_hash"],
        "attempt_state": original.last["kind"], "attempt_record_sha256": wire.digest(original.last),
        "reserved_record_sha256": wire.digest(original.reserved),
        "original_result_sha256": wire.digest(original.body), **value["delivery_binding"],
        "budget_journal_id": confirmed["data"]["budget_journal_id"],
        "stop_record_sha256": wire.digest(stopped),
        "core_stop_record_sha256": confirmed["data"]["core_stop_sha256"],
        "bridge_journal_head_sha256": wire.digest(records[-1]), "stopped_at": stopped["at"],
        "quiesced_at": records[-1]["at"]}
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(runtime.config.capture_signing_key))
    proof["signature"] = key.sign(wire.signing_payload(proof)).hex()
    runtime.db.execute(f"INSERT INTO {resolution.TABLE} VALUES (?,?,?,NULL,NULL)",
        (value["message_id"], wire.digest(original.authority), encode(proof)))
    append(records, "DELIVERY_QUIESCED", proof)
    return proof


class OnePass:
    def __init__(self, records):
        self.records, self.scans = records, 0

    def __iter__(self):
        self.scans += 1
        assert self.scans == 1, "native resolution must not rescan historical prefixes"
        yield from self.records


def test_many_historical_proofs_use_one_scan_and_keep_exact_cutoffs(custody):
    runtime, records = custody
    originals = [attempt(runtime, records, number) for number in range(1, 9)]
    for _ in range(1000):
        append(records, "PROFILE_PROBE_STARTED", {"authority": runtime.authority})
    stopped, confirmed = stop(records, runtime.authority)
    for original in originals:
        prepare(runtime, records, original, stopped, confirmed)
    before = copy.deepcopy(records)
    for _ in range(2):
        source = OnePass(records)
        resolution.verify(runtime, source)
        assert source.scans == 1
    assert records == before


@pytest.mark.parametrize("after_proof", [False, True])
def test_later_ack_cannot_change_an_earlier_quiescence_source(custody, after_proof):
    runtime, records = custody
    original = attempt(runtime, records, 1)
    stopped, confirmed = stop(records, runtime.authority)
    if after_proof:
        prepare(runtime, records, original, stopped, confirmed)
    append(records, "ACK", {"message_id": original.reserved["data"]["message_id"],
                            "provider_ack_hash": wire.digest("provider")})
    if not after_proof:
        prepare(runtime, records, original, stopped, confirmed)
        with pytest.raises(ValueError, match="DELIVERY_UNCERTAIN_SOURCE_REQUIRED"):
            resolution.verify(runtime, records)
    else:
        resolution.verify(runtime, records)


def test_handoff_keeps_the_global_prefix_and_replaces_only_later_stop_evidence(custody):
    runtime, records = custody
    old = attempt(runtime, records, 1)
    stopped, confirmed = stop(records, runtime.authority)
    prepare(runtime, records, old, stopped, confirmed)
    successor = runtime.authority | {"revision": 3}
    append(records, "OWNER_REBOUND", {"authority": successor})
    newer = attempt(runtime, records, 2, authority=successor)
    stopped, confirmed = stop(records, successor)
    prepare(runtime, records, newer, stopped, confirmed)
    resolution.verify(runtime, records)
    # An old reservation still participates after a handoff; it cannot be hidden
    # by rebuilding the attempt index only for the current authority.
    duplicate = copy.deepcopy(old.reserved)
    duplicate["data"]["message_id"] = newer.reserved["data"]["message_id"]
    records.insert(1, duplicate)
    with pytest.raises(ValueError, match="DELIVERY_UNCERTAIN_SOURCE_REQUIRED"):
        resolution.verify(runtime, records)


@pytest.mark.parametrize("mutation", ["source", "result", "signature", "head", "stop"])
def test_replay_rechecks_original_source_current_result_and_each_signature(custody, mutation):
    runtime, records = custody
    original = attempt(runtime, records, 1)
    stopped, confirmed = stop(records, runtime.authority)
    proof = prepare(runtime, records, original, stopped, confirmed)
    resolution.verify(runtime, records)
    if mutation == "source":
        original.reserved["data"]["item_hash"] = wire.digest("changed")
    elif mutation == "result":
        runtime.db.execute("UPDATE results SET body=?", (encode(original.body | {
            "lease_token": "lease_" + "f" * 32}),))
    elif mutation == "signature":
        proof["signature"] = "0" * 128
    elif mutation == "head":
        records.insert(-1, {**records[-2], "kind": "PROFILE_PROBE_STARTED"})
    else:
        stopped["at"] = (datetime(2025, 1, 1, tzinfo=UTC)).isoformat()
    error = InvalidSignature if mutation == "signature" else ValueError
    with pytest.raises(error):
        resolution.verify(runtime, records)
