"""Check original authority custody without reconciling monetary projections.

The same native ledger lock and verified journal codec protect these reads.
Financial append/settlement and operator reconciliation retain the full validator.
"""

import json
import marshal
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType

from companion import owner_authority, paid_budget
from companion.contracts import CoreError
from companion.postgres import BUDGET_LEDGER


@dataclass(frozen=True)
class _Material:
    journal_id: str | None
    authority: tuple
    lifecycle: tuple
    reservations: MappingProxyType
    invalid_reservation: bool
    size: int


@lru_cache(maxsize=1)
def _material(raw):
    """Index verified bytes, without retaining any database or permission result."""
    records, verified = paid_budget._journal_records(raw)
    previous, journal_id = "0" * 64, None
    authority, lifecycle, reservations = [], [], {}
    invalid_reservation, size = False, 0
    for index, record in enumerate(records):
        digest = record.pop("sha256")
        if not verified and (record.get("previous") != previous
                             or paid_budget.sha(record) != digest):
            raise ValueError("journal chain")
        previous = digest
        kind = record.get("kind")
        if index == 0:
            if kind != "header":
                raise ValueError("journal header required")
            journal_id = record["journal_id"]
        elif kind == owner_authority.RECORD_KIND:
            payload = marshal.dumps(record)
            authority.append((payload, digest))
            size += len(payload) + len(digest)
        elif kind in {"window_open", "window_stop", "window_handoff"}:
            payload = marshal.dumps(record)
            lifecycle.append((payload, digest))
            size += len(payload) + len(digest)
        if kind == "reserve":
            request_id = record.get("request_id")
            if not isinstance(request_id, str):
                invalid_reservation = True
                continue
            count, _ = reservations.get(request_id, (0, None))
            # Admission custody needs only these original fields, not all fee history.
            payload = marshal.dumps({key: record[key] for key in
                                     ("contract_sha256", "measurement") if key in record})
            reservations[request_id] = (count + 1, payload)
            size += len(request_id) + len(payload) + 16
    return _Material(journal_id, tuple(authority), tuple(lifecycle),
                     MappingProxyType(reservations), invalid_reservation, size)


def _read_material(raw):
    # Bounds mirror the existing chain codec and compact financial-view limits.
    # Oversized input/result is validated normally and never retained here.
    result = (_material(raw) if len(raw) <= paid_budget.CODEC_MAX_BYTES
              else _material.__wrapped__(raw))
    if len(raw) > paid_budget.CODEC_MAX_BYTES or result.size > paid_budget.CACHE_MAX_BYTES:
        _material.cache_clear()
    return result


def check_admissions(store, view, admissions):
    """Bind selected response evidence to its original reservation, without cost totals."""
    if not store.in_transaction:
        raise RuntimeError("AUTHORITY_CUSTODY_REQUIRES_TRANSACTION")
    store.ledger_lock(BUDGET_LEDGER, shared=store.in_read_snapshot)
    path = Path(view.journal_path)
    try:
        raw = path.read_bytes()
        material = _read_material(raw)
        if material.journal_id != view.journal_id or material.invalid_reservation:
            raise ValueError("journal identity")
        for row in admissions:
            count, payload = material.reservations[row["request_id"]]
            if count != 1:
                raise ValueError("duplicate original reservation")
            original = marshal.loads(payload)
            receipt = json.loads(row["measurement_json"])
            if (row["journal_id"] != view.journal_id
                or Path(row["journal_path"]).resolve() != path.resolve()
                or original["contract_sha256"] != row["contract_sha256"]
                or paid_budget.sha(json.loads(row["snapshot_json"])) != row["contract_sha256"]
                or original.get("measurement") != (
                    {"version": 1, "sha256": paid_budget.sha(receipt)} if receipt else None)
                or receipt and receipt.get("payload_sha256") != row["payload_sha256"]):
                raise ValueError("original request evidence changed")
        if path.read_bytes() != raw:
            raise ValueError("journal changed during admission check")
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
        _material.cache_clear()
        paid_budget._clear_codec()
        raise CoreError("OWNER_DELIVERY_MODEL_EVIDENCE_CHANGED", 403) from error


def check(store, revision, *, historical=False):
    if not store.in_transaction:
        raise RuntimeError("AUTHORITY_CUSTODY_REQUIRES_TRANSACTION")
    store.ledger_lock(BUDGET_LEDGER, shared=store.in_read_snapshot)
    path = Path(revision["journal_path"])
    try:
        raw = path.read_bytes()
        material = _read_material(raw)
        authority, lifecycle, seen = [], None, set()
        managed = False
        journal_id = material.journal_id
        for payload, digest in material.authority:
            owner_authority.fold(authority, marshal.loads(payload), digest)
        for payload, digest in material.lifecycle:
            record = marshal.loads(payload)
            lifecycle = paid_budget._live_record(record, digest, lifecycle, seen)
            managed |= record["kind"] != "window_stop"
        if journal_id is None:
            raise ValueError("journal header required")
        projection = revision["custody_projection"]
        if revision["custody_journal_id"] != journal_id:
            raise ValueError("journal identity")
        owner_authority._validate_projection(authority, journal_id, path, projection)
        stored = revision["custody_lifecycle"]
        if (managed or stored is not None) and (
            (json.loads(stored) if stored is not None else None) != lifecycle
        ):
            raise ValueError("live lifecycle behind journal or DB")
        if (not authority
            or historical and not any(record["record_sha256"] == revision["record_sha256"]
                                      for record in authority)
            or not historical and (authority[-1]["state"] != "ACTIVE"
                or authority[-1]["record_sha256"] != revision["record_sha256"])):
            raise ValueError("owner authority stopped")
        if path.read_bytes() != raw:
            raise ValueError("journal changed during authority check")
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
        _material.cache_clear()
        paid_budget._clear_codec()
        raise CoreError("OWNER_AUTHORITY_CUSTODY_REQUIRED", 503) from error
