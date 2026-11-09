import re
import sqlite3

from companion.legacy_import import migrate_sqlite as migrate


def assert_schema_metadata(db):
    rows = dict(db.execute("SELECT key,value FROM runtime_meta").fetchall())
    assert set(rows) == {"voice_incarnation", "owner_authority_restore_generation",
                         "owner_authority_mode"}
    assert re.fullmatch(r"[a-f0-9]{32}", rows["voice_incarnation"])
    assert rows["owner_authority_restore_generation"] == "initial"
    assert rows["owner_authority_mode"] == "OFF"


def historical_database(source, path, revision):
    """Reuse the migration fixtures' schema-column copy into a real historical DB."""
    migrate(path, revision)
    with sqlite3.connect(path) as target:
        tables = target.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                "AND name NOT LIKE 'sqlite_%' AND name!='alembic_version'")
        for (table,) in tables.fetchall():
            if table.startswith("memories_fts"):
                continue  # Native memory triggers rebuild the historical FTS index.
            if not source.has_table(table):
                continue
            columns = [row[1] for row in target.execute(f'PRAGMA table_info("{table}")')]
            for row in source.rows(f'SELECT * FROM "{table}" ORDER BY record_sequence'):
                if table == "runtime_meta" and row["key"] == "voice_incarnation":
                    continue
                if table == "runtime_meta" and row["key"] in {
                    "owner_authority_restore_generation", "owner_authority_mode",
                }:
                    # A historical fixture retains actual custody rather than a fresh
                    # migration's bootstrap marker; no runtime grant is synthesized.
                    target.execute("INSERT INTO runtime_meta(key,value) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (row["key"], row["value"]))
                    continue
                target.execute(f'INSERT INTO "{table}" ({",".join(columns)}) '
                               f'VALUES ({",".join("?" for _ in columns)})',
                               tuple(row[column] for column in columns))
        assert target.execute("PRAGMA foreign_key_check").fetchall() == []
    return path
