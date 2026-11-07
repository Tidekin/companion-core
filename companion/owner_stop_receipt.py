"""Read original journal-backed STOP evidence without granting execution permission."""

from contextlib import nullcontext

from companion import owner_authority
from companion.contracts import CoreError
from companion_delivery_protocol.authority import AuthorityRef

# Every table whose rows bind work to an owner authority revision.
BOUND_WORK = ("jobs", "goals", "exploration_requests", "model_admissions", "outbox", "voice_jobs")


def stopped_chain(store, ref):
    """The latest STOPPED revision if it closes an unbound chain started by `ref`'s STOP.

    A failed release can activate a successor and STOP it again before the Bridge hands
    off. Each intermediate ACTIVE revision must then have bound no work at all, so the
    Bridge, which never held it, cannot leave anything of it unreconciled.
    """
    original = store.one(
        'SELECT owner_authority_revisions.grant_id AS grant_id, owner_authority_revisions.revision '
        'AS revision, owner_authority_revisions.operation_id AS operation_id, '
        'owner_authority_revisions.request_sha256 AS request_sha256, '
        'owner_authority_revisions.state AS state, owner_authority_revisions.policy_json AS '
        'policy_json, owner_authority_revisions.policy_sha256 AS policy_sha256, '
        'owner_authority_revisions.policy_redacted AS policy_redacted, '
        'owner_authority_revisions.restore_generation AS restore_generation, '
        'owner_authority_revisions.predecessor_sha256 AS predecessor_sha256, '
        'owner_authority_revisions.record_sha256 AS record_sha256, '
        'owner_authority_revisions.journal_id AS journal_id, '
        'owner_authority_revisions.journal_path AS journal_path, '
        'owner_authority_revisions.created_at AS created_at FROM owner_authority_revisions AS '
        'owner_authority_revisions WHERE owner_authority_revisions.grant_id = %s AND '
        'owner_authority_revisions.revision = %s',
        (ref.grant_id, ref.revision),
    )
    latest = owner_authority._latest(store)
    if (original is None or owner_authority.reference(original) != ref
            or original["state"] != "ACTIVE" or latest is None or latest["state"] != "STOPPED"
            or latest["grant_id"] != ref.grant_id
            or (latest["revision"] - ref.revision) % 2 != 1 or latest["revision"] <= ref.revision
            or owner_authority._meta(store, owner_authority.GENERATION)
            != ref.restore_generation):
        return None
    rows = store.rows(
        'SELECT owner_authority_revisions.grant_id AS grant_id, owner_authority_revisions.revision '
        'AS revision, owner_authority_revisions.operation_id AS operation_id, '
        'owner_authority_revisions.request_sha256 AS request_sha256, '
        'owner_authority_revisions.state AS state, owner_authority_revisions.policy_json AS '
        'policy_json, owner_authority_revisions.policy_sha256 AS policy_sha256, '
        'owner_authority_revisions.policy_redacted AS policy_redacted, '
        'owner_authority_revisions.restore_generation AS restore_generation, '
        'owner_authority_revisions.predecessor_sha256 AS predecessor_sha256, '
        'owner_authority_revisions.record_sha256 AS record_sha256, '
        'owner_authority_revisions.journal_id AS journal_id, '
        'owner_authority_revisions.journal_path AS journal_path, '
        'owner_authority_revisions.created_at AS created_at FROM owner_authority_revisions AS '
        'owner_authority_revisions WHERE owner_authority_revisions.grant_id = %s AND '
        'owner_authority_revisions.revision > %s AND owner_authority_revisions.revision <= %s '
        'ORDER BY owner_authority_revisions.revision NULLS FIRST',
        (ref.grant_id, ref.revision, latest["revision"]),
    )
    previous = original
    for offset, row in enumerate(rows, start=1):
        if (row["revision"] != ref.revision + offset
                or row["state"] != ("STOPPED" if offset % 2 else "ACTIVE")
                or row["predecessor_sha256"] != previous["record_sha256"]
                or row["restore_generation"] != ref.restore_generation
                or row["state"] == "STOPPED"
                and row["policy_sha256"] != previous["policy_sha256"]):
            return None
        previous = row
    if not rows or rows[-1]["record_sha256"] != latest["record_sha256"]:
        return None
    unbound = [row["revision"] for row in rows if row["state"] == "ACTIVE"]
    for table in BOUND_WORK if unbound else ():
        placeholders = ",".join('%s' for _ in unbound)
        if store.one(
            (
                'SELECT 1 FROM '
                f'{table}'
                ' WHERE (CASE WHEN authority_json IS JSON THEN '
                "(authority_json::json#>>'{grant_id}') END)=%s AND "
                '(CASE WHEN authority_json IS JSON THEN '
                "((authority_json::json#>>'{revision}'))::numeric END) "
                'IN ('
                f'{placeholders}'
                ') LIMIT 1'
            ),
            (ref.grant_id, *unbound),
        ):
            return None
    return original, latest


def read(store, settings, authority, *, chain=False):
    """`chain` admits an unbound STOP chain; carry custody keeps the single-STOP rule."""
    ref = AuthorityRef.from_dict(authority)
    with nullcontext() if store.in_transaction else store.transaction():
        owner_authority.custody(store, settings, settlement=True)
        found = stopped_chain(store, ref)
        if found is None or not chain and found[1]["revision"] != ref.revision + 1:
            raise CoreError("OWNER_AUTHORITY_STOP_EVIDENCE_REQUIRED", 409)
        original, stopped = found
        return {"authority": ref.as_dict(), "state": "STOPPED",
                "stopped_authority": owner_authority.reference(stopped).as_dict(),
                "core_stop_sha256": stopped["record_sha256"],
                "budget_journal_id": original["journal_id"], "quiescence_proven": False}
