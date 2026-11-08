"""Release planning probe: run the target code against a private copy of production data.

The release plan starts this module inside the target Core image, with access only to
an isolated database copy, budget journal copy and erasure ledger copy.
It reports what the release would need and never touches the originals: whether the
schema must migrate (it migrates the copy and times it), whether the stored
OwnerAuthority policy stays valid under the new code and current config, and what open
work or unresolved liability a STOP would have to wait for or carry.

Output is one JSON object of codes and counts; no message text, config value or secret.
"""

import json
import time
from pathlib import Path

from companion import owner_authority
from companion.config import Settings
from companion.contracts import CoreError
from companion.ops_status import read_view, snapshot
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.store import migrate

# Blockers a migration resolves on its own; anything else means the copy is not releasable.
MIGRATION_BLOCKERS = {"MIGRATION_REQUIRED"}


def _summary(view):
    database = view.get("database", {})
    return {
        "schema": database.get("schema"),
        "expected_schema": database.get("expected_schema"),
        "integrity": database.get("integrity"),
        "blockers": sorted(view.get("blockers", [])),
        "active_jobs": view.get("jobs", {}).get("active"),
        "open_deliveries": view.get("deliveries", {}).get("active"),
        "uncertain_deliveries": view.get("deliveries", {}).get("states", {}).get(
            "DELIVERY_UNCERTAIN", 0),
        "unknown_model_calls": view.get("budget", {}).get("unknown_calls"),
    }


def authority(settings):
    """State of the latest OwnerAuthority record and whether its policy still holds."""
    with read_view(settings) as store:
        if not store.one('SELECT 1 FROM pg_catalog.pg_class WHERE '
            "relnamespace=current_schema()::regnamespace AND relname='owner_authority_revisions' "
            "AND relkind IN ('r','p')"):
            return {"state": "ABSENT", "revision": None, "policy_problem": None}
        row = owner_authority._latest(store)
        if not row:
            return {"state": "ABSENT", "revision": None, "policy_problem": None}
        if row["policy_redacted"]:
            return {"state": row["state"], "revision": row["revision"],
                    "policy_problem": "OWNER_AUTHORITY_POLICY_REDACTED"}
        policy = OwnerGrantPolicy.model_validate_json(row["policy_json"])
        problem = owner_authority.policy_problem(settings, policy)
        return {"state": row["state"], "revision": row["revision"],
                "purposes": sorted(policy.purposes),
                "expires_at": policy.expires_at.isoformat() if policy.expires_at else None,
                "policy_problem": problem.code if problem else None}


def closed_unknowns(settings):
    """Match every native UNKNOWN row to its original copied journal closure."""
    closures = {"model_local_close", "model_unknown_carry"}
    with read_view(settings) as store:
        rows = store.rows("SELECT m.request_id,a.journal_path FROM model_usage m "
                          "LEFT JOIN model_admissions a USING(request_id) WHERE m.state='UNKNOWN'")
        journals = {}
        for row in rows:
            path = row["journal_path"]
            if not path:
                return False
            if path not in journals:
                try:
                    journals[path] = [json.loads(line) for line in
                                      Path(path).read_text(encoding="utf8").splitlines()]
                except (OSError, ValueError):
                    return False
            if not any(isinstance(record, dict) and record.get("request_id") == row["request_id"]
                       and record.get("kind") in closures for record in journals[path]):
                return False
    return True


def probe(load=Settings.load):
    """`load` reads settings as the services do, including the stored operator config."""
    settings = load()
    before = _summary(snapshot(settings, integrity=True))
    result = {"schema_version": 1, "before": before, "migration": {"needed": False}}
    needed = before["schema"] != before["expected_schema"] or (
        "MIGRATION_REQUIRED" in before["blockers"])
    if needed:
        started = time.monotonic()
        try:
            migrate(settings.database)
            result["migration"] = {"needed": True, "ok": True,
                                   "seconds": round(time.monotonic() - started, 2)}
        except Exception as error:  # The plan reports it; the original data is untouched.
            result["migration"] = {"needed": True, "ok": False,
                                   "error": type(error).__name__}
            return result
        settings = load()  # The migrated copy may carry a newer operator config.
        result["after"] = _summary(snapshot(settings, integrity=True))
    try:
        result["authority"] = authority(settings)
        result["unknown_closed"] = closed_unknowns(settings)
    except (CoreError, ValueError) as error:
        result["authority"] = {"state": "UNREADABLE",
                               "error": getattr(error, "code", type(error).__name__)}
    return result


def main():
    print(json.dumps(probe(), sort_keys=True))


if __name__ == "__main__":
    main()
