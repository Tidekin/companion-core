"""Check original authority custody without reconciling monetary projections.

The same native ledger lock and verified journal codec protect these reads.
Financial append/settlement and operator reconciliation retain the full validator.
"""

import json
import marshal
import threading
from collections import OrderedDict
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
    # marshal of (authority records, live lifecycle, managed), folded once per
    # verified journal; None when the chain itself is invalid.
    chain: bytes | None
    size: int


def _fold_chain(authority, lifecycle):
    """Fold the indexed authority and lifecycle records with the original validators."""
    folded, live, seen, managed = [], None, set(), False
    for payload, digest in authority:
        owner_authority.fold(folded, marshal.loads(payload), digest)
    for payload, digest in lifecycle:
        record = marshal.loads(payload)
        live = paid_budget._live_record(record, digest, live, seen)
        managed |= record["kind"] != "window_stop"
    return folded, live, managed


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
    try:
        chain = marshal.dumps(_fold_chain(authority, lifecycle))
        size += len(chain)
    except (ValueError, TypeError, KeyError, UnicodeError):
        # Admission checks do not depend on this chain; authority checks refuse it.
        chain = None
    return _Material(journal_id, tuple(authority), tuple(lifecycle),
                     MappingProxyType(reservations), invalid_reservation, chain, size)


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


# (folded journal chain, journal path, projection digest) -> projection journal
# paths. A hit means the same journal bytes were already validated against a
# byte-identical DB projection; it is not a permission. Current state, expiry,
# purpose and file aliases are still checked on every call.
_projections = OrderedDict()
_projections_lock = threading.Lock()
_PROJECTION_ENTRIES = 4


def _check_projection(store, material, authority, journal_id, path, digest):
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("authority projection digest")
    key = (material.chain, str(path), digest)
    with _projections_lock:
        saved_paths = _projections.get(key)
        if saved_paths is not None:
            _projections.move_to_end(key)
    if saved_paths is None:
        row = store.one(owner_authority.CUSTODY_PROJECTION_SQL, (owner_authority.CURRENT,))
        if row is None or row["digest"] != digest:
            raise ValueError("authority projection changed during check")
        owner_authority._validate_projection(authority, journal_id, path, row["projection"])
        saved_paths = frozenset(item["journal_path"] for item in row["projection"])
        with _projections_lock:
            _projections[key] = saved_paths
            while len(_projections) > _PROJECTION_ENTRIES:
                _projections.popitem(last=False)
        return
    # Same rule as _validate_projection: every saved path still names this journal.
    current = path.resolve()
    if any(Path(value).resolve() != current for value in saved_paths):
        raise ValueError("authority projection mismatch")


def check(store, revision, *, historical=False):
    if not store.in_transaction:
        raise RuntimeError("AUTHORITY_CUSTODY_REQUIRES_TRANSACTION")
    store.ledger_lock(BUDGET_LEDGER, shared=store.in_read_snapshot)
    path = Path(revision["journal_path"])
    try:
        raw = path.read_bytes()
        material = _read_material(raw)
        journal_id = material.journal_id
        if material.chain is None:
            raise ValueError("authority or lifecycle chain")
        # A fresh copy per check: no caller shares or mutates the folded records.
        authority, lifecycle, managed = marshal.loads(material.chain)
        if journal_id is None:
            raise ValueError("journal header required")
        if revision["custody_journal_id"] != journal_id:
            raise ValueError("journal identity")
        _check_projection(store, material, authority, journal_id, path,
                          revision["custody_digest"])
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
        with _projections_lock:
            _projections.clear()
        paid_budget._clear_codec()
        raise CoreError("OWNER_AUTHORITY_CUSTODY_REQUIRED", 503) from error
