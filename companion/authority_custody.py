"""Check original authority custody without reconciling monetary projections.

The same native ledger lock and verified journal codec protect these reads.
Financial append/settlement and operator reconciliation retain the full validator.
"""

import json
from pathlib import Path

from companion import owner_authority, paid_budget
from companion.contracts import CoreError
from companion.postgres import BUDGET_LEDGER


def check_admissions(store, view, admissions):
    """Bind selected response evidence to its original reservation, without cost totals."""
    if not store.in_transaction:
        raise RuntimeError("AUTHORITY_CUSTODY_REQUIRES_TRANSACTION")
    store.ledger_lock(BUDGET_LEDGER, shared=store.in_read_snapshot)
    path = Path(view.journal_path)
    try:
        raw = path.read_bytes()
        records, verified = paid_budget._journal_records(raw)
        previous, journal_id, reservations = "0" * 64, None, {}
        selected = {row["request_id"] for row in admissions}
        for index, record in enumerate(records):
            digest = record.pop("sha256")
            if not verified and (record.get("previous") != previous
                                 or paid_budget.sha(record) != digest):
                raise ValueError("journal chain")
            previous = digest
            if index == 0:
                if record.get("kind") != "header":
                    raise ValueError("journal header required")
                journal_id = record["journal_id"]
            if record.get("kind") == "reserve" and record["request_id"] in selected:
                if record["request_id"] in reservations:
                    raise ValueError("duplicate original reservation")
                reservations[record["request_id"]] = record
        if journal_id != view.journal_id:
            raise ValueError("journal identity")
        for row in admissions:
            original = reservations[row["request_id"]]
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
        paid_budget._clear_codec()
        raise CoreError("OWNER_DELIVERY_MODEL_EVIDENCE_CHANGED", 403) from error


def check(store, revision, *, historical=False):
    if not store.in_transaction:
        raise RuntimeError("AUTHORITY_CUSTODY_REQUIRES_TRANSACTION")
    store.ledger_lock(BUDGET_LEDGER, shared=store.in_read_snapshot)
    path = Path(revision["journal_path"])
    try:
        raw = path.read_bytes()
        records, verified = paid_budget._journal_records(raw)
        authority, lifecycle, seen = [], None, set()
        managed = False
        previous, journal_id = "0" * 64, None
        for index, record in enumerate(records):
            digest = record.pop("sha256")
            if not verified and (
                record.get("previous") != previous or paid_budget.sha(record) != digest
            ):
                raise ValueError("journal chain")
            previous = digest
            if index == 0:
                if record.get("kind") != "header":
                    raise ValueError("journal header required")
                journal_id = record["journal_id"]
            elif record.get("kind") == owner_authority.RECORD_KIND:
                owner_authority.fold(authority, record, digest)
            elif record.get("kind") in {"window_open", "window_stop", "window_handoff"}:
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
        paid_budget._clear_codec()
        raise CoreError("OWNER_AUTHORITY_CUSTODY_REQUIRED", 503) from error
