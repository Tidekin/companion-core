"""One-shot import of a stopped Core SQLite backup into an empty PostgreSQL baseline.

Only the offline migration operator calls this module. It never dispatches jobs,
replays provider requests, or reads/writes the independent cost and erasure journals.
Native COPY and a single transaction retain original values, keys and row ordering.
"""

import sqlite3
from itertools import chain, zip_longest
from pathlib import Path

import psycopg
from psycopg import sql
from pydantic import SecretStr

SOURCE_REVISION = "0068_image_review_target"
TARGET_REVISION = "pg0005_companion_profile"
# Native-only tables created after the SQLite era; an offline import leaves them empty.
NATIVE_ONLY_TABLES = frozenset({"companion_profile", "companion_profile_settings",
                                "companion_profile_changes"})


def migrate_sqlite(path, revision="head"):
    from alembic import command
    from alembic.config import Config

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    migrations = Path(__file__).parent.parent / "migrations"
    if not migrations.exists():
        migrations = Path(__file__).parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations))
    cfg.set_main_option("sqlalchemy.url", "sqlite:///" + Path(path).resolve().as_posix())
    command.upgrade(cfg, revision)

def import_sqlite_backup(path, conninfo: SecretStr):
    """Import once, compare every business row before commit, and return content-free counts.

    The release operator must hold native STOP and retain the complete backup plus
    independent journals. This function does not switch authority or start workers.
    A failed operation is rolled back, never automatically retried.
    """
    if not isinstance(conninfo, SecretStr):
        raise TypeError("database connection information must be SecretStr")
    source_path = Path(path).resolve(strict=True)
    source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        if source.execute("SELECT version_num FROM alembic_version").fetchall() != [
            (SOURCE_REVISION,)
        ]:
            raise RuntimeError("SQLITE_IMPORT_SOURCE_REVISION_MISMATCH")
        if source.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLITE_IMPORT_SOURCE_INTEGRITY_FAILED")
        if source.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("SQLITE_IMPORT_SOURCE_FOREIGN_KEY_FAILED")
        tables = [row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE 'memories_fts%' AND name!='alembic_version' ORDER BY name")]
        with psycopg.connect(conninfo.get_secret_value(), connect_timeout=5) as target:
            if target.execute("SELECT version_num FROM alembic_version").fetchall() != [
                (TARGET_REVISION,)
            ]:
                raise RuntimeError("SQLITE_IMPORT_TARGET_REVISION_MISMATCH")
            actual = [row[0] for row in target.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_type='BASE TABLE' "
                "AND table_name!='alembic_version' ORDER BY table_name")]
            native_only = [name for name in actual if name in NATIVE_ONLY_TABLES]
            actual = [name for name in actual if name not in NATIVE_ONLY_TABLES]
            if actual != tables:
                raise RuntimeError("SQLITE_IMPORT_TABLE_SET_MISMATCH")
            # This is an offline import, not a lock used by the running Core.
            target.execute("SET LOCAL lock_timeout='5s'")
            target.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
                sql.SQL(",").join(sql.Identifier(name) for name in tables)))
            target.execute("SET CONSTRAINTS ALL DEFERRED")
            _require_empty(target, [*tables, *native_only])
            columns_by_table = {}
            for table, column in target.execute(
                "SELECT table_name,column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema()"
            ):
                columns_by_table.setdefault(table, set()).add(column)
            counts = {}
            for name in tables:
                counts[name] = _copy_table(source, target, name, columns_by_table[name])
            # Check cyclic references now, then compare source and target values.
            target.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for name in tables:
                if counts[name]:
                    _compare_table(source, target, name)
            _require_empty(target, [name for name in tables if not counts[name]])
            return {"source_revision": SOURCE_REVISION, "target_revision": TARGET_REVISION,
                    "tables": counts, "business_rows_compared": sum(counts.values())}
    finally:
        source.close()


def _columns(source, name):
    # Names originate in sqlite_master, and are matched against the native target.
    quoted = '"' + name.replace('"', '""') + '"'
    return quoted, [row[1] for row in source.execute(f"PRAGMA table_info({quoted})")]


def _require_empty(target, tables):
    if tables:
        statement = sql.SQL(" UNION ALL ").join(
            sql.SQL("SELECT EXISTS(SELECT 1 FROM {})").format(sql.Identifier(name))
            for name in tables
        )
        if any(row[0] for row in target.execute(statement)):
            raise RuntimeError("SQLITE_IMPORT_TARGET_NOT_EMPTY")


def _copy_table(source, target, name, actual):
    identifier = sql.Identifier(name)
    quoted, columns = _columns(source, name)
    projection = {"preview_json", "preview_version"} if name == "agent_conversations" else set()
    if actual != set(columns) | {"record_sequence"} | projection:
        raise RuntimeError("SQLITE_IMPORT_COLUMN_SET_MISMATCH")
    rows = source.execute(f"SELECT rowid,* FROM {quoted} ORDER BY rowid")
    first = rows.fetchone()
    if first is None and name != "audit_log":
        return 0
    count = 0
    if first is not None:
        with target.cursor().copy(sql.SQL("COPY {} ({}) FROM STDIN").format(
            identifier, sql.SQL(",").join(sql.Identifier(column) for column in
                                        ["record_sequence", *columns]))) as copy:
            for row in chain([first], rows):
                copy.write_row(row)
                count += 1
    for column in ["record_sequence", *(["audit_id"] if name == "audit_log" else [])]:
        maximum = target.execute(sql.SQL("SELECT max({}) FROM {}").format(
            sql.Identifier(column), identifier)).fetchone()[0]
        if name == "audit_log":
            original = source.execute("SELECT seq FROM sqlite_sequence WHERE name='audit_log'")
            highwater = original.fetchone()
            if highwater:
                maximum = max(maximum or 0, highwater[0])
        sequence = target.execute("SELECT pg_get_serial_sequence(%s,%s)", (name, column))
        populated = maximum is not None and maximum > 0
        target.execute("SELECT setval(%s,%s,%s)",
                       (sequence.fetchone()[0], maximum if populated else 1, populated))
    return count


def _compare_table(source, target, name):
    quoted, columns = _columns(source, name)
    if name == "agent_conversations" and target.execute(
        "SELECT 1 FROM agent_conversations WHERE preview_json IS NOT NULL OR preview_version<>0"
    ).fetchone():
        raise RuntimeError("SQLITE_IMPORT_ROW_RECONCILIATION_FAILED")
    statement = sql.SQL("SELECT {} FROM {} ORDER BY record_sequence").format(
        sql.SQL(",").join(sql.Identifier(column) for column in ["record_sequence", *columns]),
        sql.Identifier(name))
    missing = object()
    with target.cursor(name="compare_" + name) as rows:
        rows.itersize = 8
        rows.execute(statement)
        for before, after in zip_longest(
            source.execute(f"SELECT rowid,* FROM {quoted} ORDER BY rowid"), rows,
            fillvalue=missing,
        ):
            if before != after:
                raise RuntimeError("SQLITE_IMPORT_ROW_RECONCILIATION_FAILED")
