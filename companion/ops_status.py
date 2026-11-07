"""Content-free diagnostics and the native full runtime status.

The read-only snapshot never bootstraps, migrates, replays or repairs state.
The full runtime view retains the existing live custody checks on its caller's Store.
"""

import json
import shutil
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from alembic.script import ScriptDirectory
from alembic.util import CommandError
from psycopg import Error as PostgresError
from psycopg_pool import PoolTimeout

from companion import __version__, budget, paid_budget
from companion.contracts import CoreError
from companion.postgres import ERASURE_LEDGER, Database
from companion.privacy import ErasureLedger
from companion.store import Store


@contextmanager
def read_view(settings):
    """Finite native snapshot without migration, bootstrap, replay or write retry."""
    with Database(settings.database, max_connections=1) as database:
        with closing(Store(database, settings.state_directory)) as store, store.read_snapshot():
            store.execute("SET LOCAL statement_timeout='2s'")
            store.execute("SET LOCAL lock_timeout='1s'")
            yield store


def _migration_scripts(*, legacy=False):
    root = Path(__file__).resolve().parent
    migrations = root.parent / "migrations"
    if not migrations.is_dir():
        migrations = root / "migrations"
    return ScriptDirectory(str(migrations if legacy else migrations / "postgresql"))


@lru_cache(maxsize=1)
def schema_head():
    try:
        head = _migration_scripts().get_current_head()
    except (OSError, CommandError) as error:
        raise CoreError("SCHEMA_HEAD_UNAVAILABLE", 503) from error
    if not isinstance(head, str) or not head:
        raise CoreError("SCHEMA_HEAD_UNAVAILABLE", 503)
    return head


@lru_cache(maxsize=1)
def _packaged_revisions(head):
    # Packaged SQLite revisions remain known predecessors after the one-way import.
    # They must never be treated as an unknown, future PostgreSQL schema.
    return frozenset(
        rev.revision for scripts, start in (
            (_migration_scripts(), head), (_migration_scripts(legacy=True), "heads"),
        ) for rev in scripts.walk_revisions(head=start)
    )


def schema_readiness(view):
    versions = []
    if view.has_table("alembic_version"):
        versions = view.rows("SELECT version_num FROM alembic_version LIMIT 2")
    version = versions[0]["version_num"] if len(versions) == 1 else None
    details = {"schema": version, "expected_schema": None}
    try:
        head = details["expected_schema"] = schema_head()
        capable = details["capabilities_present"] = Store.schema_capabilities(view)
        # A rollback may encounter a newer compatible schema. Known ancestors need migration.
        if capable and version and (version == head or version not in _packaged_revisions(head)):
            return details, None
    except CoreError as error:
        return details, error.code
    except (OSError, CommandError):
        return details, "SCHEMA_HEAD_UNAVAILABLE"
    return details, "MIGRATION_REQUIRED"


def _resource(path):
    try:
        stat = Path(path).stat()
        return {"exists": True, "bytes": stat.st_size}
    except OSError:
        return {"exists": False, "bytes": None}


def _queue(view, table, active, now):
    states = {r["state"]: r["n"] for r in view.rows(
        f"SELECT state,COUNT(*) n FROM {table} GROUP BY state"
    )}
    oldest = view.one(
        f"SELECT MIN(created_at) at FROM {table} WHERE state=ANY(%s::text[])", (list(active),))
    age = None
    if oldest["at"]:
        age = max(0, (now - datetime.fromisoformat(oldest["at"])).total_seconds())
    expired = view.one(
        f"SELECT COUNT(*) n FROM {table} WHERE state=ANY(%s::text[]) "
        "AND lease_until IS NOT NULL AND lease_until::timestamptz<=%s::timestamptz",
        (list(active), now.isoformat()),
    )["n"]
    return {"states": states, "active": sum(states.get(s, 0) for s in active),
            "oldest_age_seconds": age, "expired_leases": expired}


def _ledger(view, settings, result, integrity):
    view.ledger_lock(ERASURE_LEDGER, shared=True)
    resource = _resource(settings.erasure_ledger)
    result["resources"]["erasure_ledger"] = resource
    identity = view.one("SELECT value FROM runtime_meta WHERE key = 'ledger_id'")
    if not identity:
        result["blockers"].append("ERASURE_IDENTITY_UNINITIALIZED")
        return
    try:
        with Path(settings.erasure_ledger).open("rb") as stream:
            header = json.loads(stream.readline(4096))
        if not isinstance(header, dict) or header.get("ledger_id") != identity["value"]:
            raise ValueError("identity")
        resource["checked"] = "HEADER_ONLY"
    except (OSError, ValueError, TypeError):
        result["blockers"].append("ERASURE_LEDGER_RECONCILIATION_REQUIRED")
        return
    if view.one("SELECT COUNT(*) AS n FROM privacy_erasure_ledger WHERE state <> 'COMPLETE'")["n"]:
        result["blockers"].append("ERASURE_REPLAY_REQUIRED")
    if not integrity:
        return
    if not resource["exists"] or resource["bytes"] > 4 * 1024 * 1024:
        result["blockers"].append("ERASURE_LEDGER_UNAVAILABLE_OR_TOO_LARGE")
        return
    try:
        records = ErasureLedger(settings.erasure_ledger).read()
        if records[0]["ledger_id"] != identity["value"]:
            raise CoreError("ERASURE_LEDGER_MISMATCH", 503)
        applied = {r["erasure_id"]: r for r in view.rows(
            'SELECT erasure_id, restore_min_replay_version, state FROM privacy_erasure_ledger'
        )}
        if len(applied) != len(records) - 1 or any(
            not (row := applied.get(record["erasure_id"])) or row["state"] != "COMPLETE"
            or row["restore_min_replay_version"] != record["seq"]
            for record in records[1:]
        ):
            raise CoreError("ERASURE_REPLAY_REQUIRED", 503)
        resource["records"] = len(records) - 1
        resource["checked"] = "FULL_LEDGER"
    except (CoreError, KeyError, TypeError):
        result["blockers"].append("ERASURE_LEDGER_RECONCILIATION_REQUIRED")


def _budget_journal(view, settings, result, integrity):
    identity = view.one("SELECT value FROM runtime_meta WHERE key = 'model_budget_journal_id'")
    paths = {r["journal_path"] for r in view.rows(
        'SELECT DISTINCT journal_path FROM model_admissions'
    )}
    if settings.paid_model:
        paths.add(settings.paid_model.journal_path)
    if not paths and not identity:
        result["resources"]["budget_journal"] = {"required": False, "checked": False}
        return
    resource = {"required": True, "checked": False}
    result["resources"]["budget_journal"] = resource
    if not identity or len(paths) != 1:
        result["blockers"].append("BUDGET_JOURNAL_RECONCILIATION_REQUIRED")
        return
    path = paths.pop()
    resource.update(_resource(path))
    if not resource["exists"]:
        result["blockers"].append("BUDGET_JOURNAL_UNAVAILABLE_OR_TOO_LARGE")
        return
    try:
        with Path(path).open("rb") as stream:
            header = json.loads(stream.readline(4096))
        if not isinstance(header, dict) or header.get("journal_id") != identity["value"]:
            raise ValueError("identity")
        resource["checked"] = "HEADER_ONLY"
    except (OSError, ValueError, TypeError):
        result["blockers"].append("BUDGET_JOURNAL_RECONCILIATION_REQUIRED")
        return
    if not integrity:
        return
    if resource["bytes"] > 4 * 1024 * 1024:
        result["blockers"].append("BUDGET_JOURNAL_UNAVAILABLE_OR_TOO_LARGE")
        return
    try:
        # Identity is already present in this read transaction. journal(record=None)
        # only checks; it cannot take its absent-identity initialization branch.
        from companion.paid_budget import journal

        journal(view, SimpleNamespace(journal_path=path), initialize=False)
        resource["checked"] = "FULL_JOURNAL"
    except CoreError:
        result["blockers"].append("BUDGET_JOURNAL_RECONCILIATION_REQUIRED")


def snapshot(settings, *, integrity=False):
    """Native read-only diagnostics; integrity reports catalog checks explicitly."""
    now = datetime.now(UTC)
    result = {"schema_version": 1, "version": __version__, "mode": settings.mode,
              "observed_at": now.isoformat(), "read_only": True, "blockers": [],
              "worker_progress": {"state": "UNOBSERVED",
                                  "reason": "BUSINESS_PROGRESS_NOT_INFERRED"},
              "worker_loop": {"state": "UNOBSERVED"},
              "database": {"storage": "postgresql", "exists": False, "bytes": None,
                           "wal_bytes": None, "schema": None,
                           "expected_schema": None}, "resources": {}}
    try:
        result["resources"]["disk_free_bytes"] = shutil.disk_usage(
            Path(settings.state_directory).resolve()).free
    except OSError:
        result["resources"]["disk_free_bytes"] = None
        result["blockers"].append("DISK_RESOURCE_UNAVAILABLE")
    try:
        with read_view(settings) as view:
            result["database"].update(exists=True, bytes=view.one(
                "SELECT pg_database_size(current_database()) AS n")["n"])
            readiness, blocker = schema_readiness(view)
            result["database"].update(readiness)
            if blocker:
                result["blockers"].append(blocker)
                return result
            from companion.worker_loop import snapshot as loop_snapshot

            result["worker_loop"] = loop_snapshot(view, now)
            result["database"]["integrity"] = "NOT_CHECKED"
            if integrity:
                invalid = view.one(
                    "SELECT 1 FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace "
                    "WHERE n.nspname=current_schema() AND NOT c.convalidated LIMIT 1") or view.one(
                    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=current_schema() AND (NOT i.indisvalid OR NOT i.indisready) "
                    "LIMIT 1")
                result["database"]["integrity"] = "FAILED" if invalid else "CATALOG_VALID"
                result["database"]["integrity_basis"] = "catalog_constraints_and_indexes"
                if invalid:
                    result["blockers"].append("DATABASE_INTEGRITY_FAILED")
            owner = view.one("SELECT value FROM runtime_meta WHERE key = 'owner_qq_id'")
            if not owner or owner["value"] != settings.owner_qq_id:
                result["blockers"].append("OWNER_IDENTITY_UNINITIALIZED_OR_MISMATCH")
            _ledger(view, settings, result, integrity)
            _budget_journal(view, settings, result, integrity)
            result["jobs"] = _queue(view, "jobs", ("QUEUED", "RUNNING", "RETRY_WAIT"), now)
            result["deliveries"] = _queue(view, "outbox", (
                "PENDING", "LEASED", "SENDING", "RETRY_WAIT", "DELIVERY_UNCERTAIN",
            ), now)
            result["budget"] = view.one(
                "SELECT COUNT(*) unsettled_calls,COALESCE(SUM(billed_micro_usd),0)::bigint "
                "unsettled_micro_usd,COUNT(*) FILTER (WHERE state='UNKNOWN') unknown_calls "
                "FROM model_usage WHERE state IN ('UNKNOWN','RESERVED')"
            )
            result["budget"]["pricing_configured"] = settings.pricing_known
            result["budget"]["paid_approval_configured"] = settings.paid_model is not None
            result["budget"]["unlimited_period_spend"] = bool(
                settings.paid_model and settings.paid_model.unlimited_period_spend,
            )
            result["budget"]["calibration"] = budget.usage_diagnostics(view)
            if settings.mode == "live" and settings.paid_model is None:
                result["blockers"].append("PAID_APPROVAL_REQUIRED")
            for key in (
                "model_budget_blocked", "model_budget_restore_required",
            ):
                if view.one('SELECT 1 FROM runtime_meta WHERE key = %s', (key,)):
                    result["blockers"].append(key.upper())
            from companion.live_text import stopped

            lifecycle = paid_budget.live_lifecycle(view)
            plan = settings.live_text_window.plan_sha256 if (
                settings.live_text_window and lifecycle and lifecycle["state"] == "OPEN"
            ) else None
            if stopped(view, plan):
                result["blockers"].append("LIVE_TEXT_STOPPED")
            if result["budget"]["unsettled_calls"]:
                result["blockers"].append("MODEL_USAGE_UNSETTLED")
            if result["deliveries"]["states"].get("DELIVERY_UNCERTAIN"):
                result["blockers"].append("DELIVERY_UNCERTAIN")
            if result["jobs"]["expired_leases"] or result["deliveries"]["expired_leases"]:
                result["blockers"].append("EXPIRED_LEASES")
            result["resources"]["embedding_cache"] = {
                "required": settings.memory_enabled,
                "exists": Path(settings.embedding_cache_dir).is_dir(),
                "model_loaded": False,
            }
            if settings.memory_enabled and not Path(settings.embedding_cache_dir).is_dir():
                result["blockers"].append("EMBEDDING_CACHE_MISSING")
    except (PostgresError, PoolTimeout, OSError, ValueError, TypeError):
        result["blockers"].append("DATABASE_SNAPSHOT_UNAVAILABLE")
    return result


def runtime_snapshot(store, settings, clock):
    """Full native status on the caller-owned connection, without service bootstrap."""
    from companion import live_text
    from companion.consolidation import enabled as consolidation_enabled
    from companion.diary import DiaryRepository

    s = store
    return {
        "version": __version__,
        "mode": settings.mode,
        "operations": snapshot(settings),
        "live_text_window": live_text.status(s, settings, clock),
        "features": {
            "memory": settings.memory_enabled,
            "diary": DiaryRepository(store, settings, clock).enabled(),
            "diary_auto_share": False,
            "affect": settings.affect_enabled,
            "consolidation": consolidation_enabled(store, settings, clock),
            "reflection": settings.reflection_enabled,
            "goals": settings.goals_enabled,
            "wake": settings.wake_enabled,
            "executive": settings.executive.enabled,
            "dsh": settings.executive.enabled
            and settings.executive.allow_changes
            and any(
                "dsh.run" in endpoint.capabilities
                for endpoint in settings.executive.endpoints.values()
            ),
            "media": settings.media.enabled,
            "content": settings.content_enabled,
            "voice": settings.media.enabled and settings.voice_enabled,
            "camera": settings.media.enabled and settings.camera_enabled,
            "android": settings.media.enabled and settings.android_enabled,
            "public_publish": False,
        },
        "jobs": s.rows('SELECT state, COUNT(*) AS count FROM jobs GROUP BY state'),
        "deliveries": s.rows('SELECT state, COUNT(*) AS count FROM outbox GROUP BY state'),
        "tool_usage": s.rows(
            "SELECT capability,state,COUNT(*) AS calls,"
            "SUM(billed_micro_usd)::bigint AS estimated_micro_usd FROM tool_tasks "
            "GROUP BY capability,state"
        ),
        "usage": s.rows(
            "SELECT scenario,SUM(input_tokens)::bigint AS input_tokens,"
            "SUM(output_tokens)::bigint AS output_tokens,"
            "SUM(cached_input_tokens)::bigint AS cached_tokens,"
            "SUM(billed_micro_usd)::bigint AS estimated_micro_usd,"
            "COUNT(*) FILTER (WHERE pricing_known=0) AS unknown_price_requests,"
            "COUNT(*) FILTER (WHERE state='UNKNOWN') AS unknown_usage_requests "
            "FROM model_usage GROUP BY scenario"
        ),
    }
