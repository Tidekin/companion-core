"""Native PostgreSQL baseline: accounting types, evidence and concurrent constraints."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

from companion.legacy_import import import_sqlite_backup
from companion.legacy_import import migrate_sqlite as migrate_legacy
from companion.postgres import migrate
from tests.postgres_fixture import isolated_database, isolated_native


@pytest.fixture(scope="module")
def native():
    with isolated_native() as value:
        yield value


@pytest.fixture
def empty_native():
    with isolated_native() as value:
        yield value


def test_baseline_preserves_canonical_business_columns_and_foreign_keys(native, tmp_path):
    path = tmp_path / "canonical.sqlite3"
    migrate_legacy(path, "0068_image_review_target")
    with sqlite3.connect(path) as legacy, native[0].session() as session:
        source = [row[0] for row in legacy.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE 'memories_fts%' AND name!='alembic_version' ORDER BY name")]
        target = [row["table_name"] for row in session.rows(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() "
            "AND table_type='BASE TABLE' AND table_name!='alembic_version' ORDER BY table_name")]
        assert target == source
        column_types = {}
        for row in session.rows(
            "SELECT table_name,column_name,data_type FROM information_schema.columns "
            "WHERE table_schema=current_schema()"
        ):
            column_types.setdefault(row["table_name"], {})[row["column_name"]] = row["data_type"]
        foreign_keys = 0
        for name in source:
            expected = {row[1]: row[2] for row in legacy.execute(f'PRAGMA table_info("{name}")')}
            actual = column_types[name]
            assert set(actual) == set(expected) | {"record_sequence"}, name
            for column, kind in expected.items():
                assert actual[column] == {
                    "INTEGER": "bigint", "TEXT": "text", "BLOB": "bytea",
                    "REAL": "double precision",
                }[kind.upper()], (name, column)
            foreign_keys += len({row[0] for row in legacy.execute(
                f'PRAGMA foreign_key_list("{name}")')})
        assert session.one(
            "SELECT count(*) AS n FROM information_schema.table_constraints "
            "WHERE constraint_schema=current_schema() AND constraint_type='FOREIGN KEY'"
        )["n"] == foreign_keys
        assert session.one("SELECT version_num FROM alembic_version") == {
            "version_num": "pg0003_fact_key_ascii"}


def test_fresh_baseline_and_existing_database_have_the_ordered_relationship_index():
    def directions(db):
        return db.execute(
            "SELECT pg_index_column_has_property('idx_memories_relationship_standing'::regclass, "
            "position, 'desc') FROM generate_series(1,3) AS position ORDER BY position"
        ).fetchall()

    expected = [(True,), (True,), (False,)]
    with isolated_database() as info:
        migrate(info, revision="pg0001_core")
        with psycopg.connect(info.get_secret_value()) as db:
            assert directions(db) == expected
            # Recreate the index definition shipped before pg0002.
            db.execute("DROP INDEX idx_memories_relationship_standing")
            db.execute("CREATE INDEX idx_memories_relationship_standing ON memories "
                       "(importance,updated_at,memory_id) WHERE subject='relationship' "
                       "AND status='CONFIRMED' AND needs_review=0 AND kind<>'episode' "
                       "AND origin<>'event_observed' AND privacy_scope='private' AND confidence>0")
            assert directions(db) == [(False,), (False,), (False,)]
        migrate(info)
        with psycopg.connect(info.get_secret_value()) as db:
            assert directions(db) == expected
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "pg0003_fact_key_ascii",)


def test_fact_key_index_upgrades_from_relationship_revision():
    statement = (
        "SELECT pg_get_expr(indexprs,indrelid) FROM pg_index "
        "WHERE indexrelid='idx_memories_fact_key'::regclass"
    )
    with isolated_database() as info:
        migrate(info, revision="pg0002_relationship_standing")
        with psycopg.connect(info.get_secret_value()) as db:
            assert "lower(" in db.execute(statement).fetchone()[0]
        migrate(info)
        with psycopg.connect(info.get_secret_value()) as db:
            expression = db.execute(statement).fetchone()[0]
            assert "translate(" in expression
            assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" in expression
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "pg0003_fact_key_ascii",)


def audit(session, document):
    return session.one(
        "INSERT INTO audit_log (action,actor_id,entity_type,entity_id,trace_id,safe_detail_json,"
        "created_at) VALUES ('synthetic','system','core','synthetic','trace',%s,'2026-01-01') "
        "RETURNING audit_id,safe_detail_json,record_sequence", (document,))


def test_json_evidence_keeps_exact_bytes_and_upgrade_is_idempotent(native):
    document = '{ "a": 1, "a":2, "汉字": [true, null] }'
    with native[0].session() as session:
        original = audit(session, document)
    migrate(native[1])
    with native[0].session() as session:
        assert session.one("SELECT safe_detail_json FROM audit_log WHERE audit_id=%s",
                           (original["audit_id"],)) == {"safe_detail_json": document}
        with pytest.raises(psycopg.errors.CheckViolation), session.transaction():
            audit(session, "{corrupt")


def memory(session, memory_id):
    session.execute(
        "INSERT INTO memories (memory_id,subject,kind,content,status,origin,confidence,importance,"
        "privacy_scope,created_at,updated_at) VALUES "
        "(%s,'user','fact','小鲸鱼喜欢吃白饭','CONFIRMED','user_confirmed',1,1,'private','now','now')",
        (memory_id,))


def vector(session, memory_id):
    session.execute("INSERT INTO memory_vectors (memory_id,model_version,content_hash,vector) "
                    "VALUES (%s,'synthetic','hash',%s)", (memory_id, b"\x00\xff\x80"))


def test_content_change_and_forgetting_invalidate_derived_vectors(native):
    memory_id = str(uuid4())
    with native[0].session() as session, session.transaction():
        memory(session, memory_id)
        vector(session, memory_id)
        assert session.one("SELECT vector FROM memory_vectors WHERE memory_id=%s",
                           (memory_id,)) == {"vector": b"\x00\xff\x80"}
        session.execute("UPDATE memories SET content='小鲸鱼吃面' WHERE memory_id=%s", (memory_id,))
        assert session.one("SELECT * FROM memory_vectors WHERE memory_id=%s", (memory_id,)) is None
        vector(session, memory_id)
        session.execute("UPDATE memories SET status='FORGOTTEN' WHERE memory_id=%s", (memory_id,))
        assert session.one("SELECT * FROM memory_vectors WHERE memory_id=%s", (memory_id,)) is None
        assert session.one("SELECT memory_id FROM memories WHERE memory_id=%s "
                           "AND status IN ('CONFIRMED','HYPOTHESIS') AND privacy_scope='private' "
                           "AND confidence>0 AND content ILIKE %s", (memory_id, "%小鲸鱼%")) is None


def goal(session, goal_id):
    session.execute(
        "INSERT INTO goals (goal_id,kind,topic,scope_json,proposal_key,state,priority,next_due_at,"
        "created_at,updated_at) VALUES "
        "(%s,'self','synthetic','{}',%s,'ACCEPTED',1,'now','now','now')",
        (goal_id, goal_id))


def step(session, goal_id, position):
    session.execute(
        "INSERT INTO goal_steps "
        "(step_id,goal_id,position,state,max_attempts,created_at,updated_at) "
        "VALUES (%s,%s,%s,'QUEUED',1,'now','now')", (str(uuid4()), goal_id, position))


def test_concurrent_inserts_cannot_admit_a_fourth_goal_step(native):
    goal_id = str(uuid4())
    with native[0].session() as session, session.transaction():
        goal(session, goal_id)
        step(session, goal_id, 1)
        step(session, goal_id, 2)
    barrier = Barrier(2, timeout=10)

    def insert(position):
        try:
            with native[0].session() as session, session.transaction():
                session.execute("SET LOCAL statement_timeout='10s'")
                barrier.wait()
                step(session, goal_id, position)
            return "committed"
        except psycopg.errors.CheckViolation as error:
            assert "GOAL_STEP_LIMIT" in str(error)
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(insert, [3, 4])) == ["committed", "rejected"]
    with native[0].session() as session:
        assert session.one("SELECT count(*) AS n FROM goal_steps WHERE goal_id=%s", (goal_id,)) == {
            "n": 3}


def test_goal_step_json_requires_an_object(native):
    goal_id = str(uuid4())
    with native[0].session() as session:
        goal(session, goal_id)
        for document in ("null", "[]", "{broken"):
            with pytest.raises(psycopg.errors.CheckViolation), session.transaction():
                session.execute(
                    "INSERT INTO goal_steps (step_id,goal_id,scope_json,state,max_attempts,"
                    "created_at,updated_at) VALUES (%s,%s,%s,'QUEUED',1,'now','now')",
                    (str(uuid4()), goal_id, document))


def test_stale_repeatable_read_cannot_admit_a_fourth_goal_step(native):
    goal_id = str(uuid4())
    with native[0].session() as session, session.transaction():
        goal(session, goal_id)
        step(session, goal_id, 1)
        step(session, goal_id, 2)
    with native[0].session() as first, native[0].session() as second:
        with pytest.raises(psycopg.errors.SerializationFailure), first.transaction():
            first.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            assert first.one("SELECT count(*) AS n FROM goal_steps WHERE goal_id=%s",
                             (goal_id,)) == {"n": 2}
            with second.transaction():
                step(second, goal_id, 3)
            step(first, goal_id, 4)


def legacy_backup(path):
    migrate_legacy(path, "0068_image_review_target")
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")

    def insert(table, **values):
        names = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        connection.execute(f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                           tuple(values.values()))

    try:
        insert("audit_log", action="synthetic", actor_id="system", entity_type="core",
               entity_id="entity", trace_id="trace", safe_detail_json='{"value":"原文"}',
               created_at="now")
        high = dict(connection.execute("SELECT * FROM audit_log").fetchone())
        high["audit_id"] = 2**40
        insert("audit_log", **high)
        connection.execute("DELETE FROM audit_log WHERE audit_id=?", (2**40,))
        insert("model_usage", usage_id="usage", request_id="request", scenario="chat",
                     provider="deepseek", model="deepseek-flash", occurred_at="now",
                     state="UNKNOWN", billed_micro_usd=2**40)
        insert("memories", memory_id="memory", subject="user", kind="fact",
                     content="小鲸鱼喜欢吃白饭", status="CONFIRMED", origin="user_confirmed",
                     confidence=1, importance=1, privacy_scope="private",
                     created_at="now", updated_at="now")
        insert("memory_vectors", memory_id="memory", model_version="synthetic",
                     content_hash="hash", vector=b"\x00\xff\x80")
        insert("runtime_meta", key="synthetic_evidence", value='{ "x": 1, "x": 2 }')
        insert("principals", actor_id="owner", kind="owner", created_at="now")
        insert("conversations", conversation_id="conversation", platform="qq",
                     platform_session_key="synthetic", scope="private", owner_actor_id="owner",
                     created_at="now")
        document = '{"text":"保留未明投递"}'
        insert("outbox", message_id="message", conversation_id="conversation",
                     action_type="qq.private.reply", content_json=document,
                     content_sha256=hashlib.sha256(document.encode()).hexdigest(),
                     state="DELIVERY_UNCERTAIN", delivery_key="native-original-key",
                     attempt_count=1, created_at="now", updated_at="now")
    finally:
        connection.close()


def test_offline_copy_preserves_values_order_and_unknown_fees(empty_native, tmp_path):
    source = tmp_path / "stopped-backup.sqlite3"
    legacy_backup(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    receipt = import_sqlite_backup(source, empty_native[1])
    assert receipt["tables"]["model_usage"] == 1
    # Eight fixture rows plus the three authority/incarnation rows from revision 0068.
    assert receipt["business_rows_compared"] == 11
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    with empty_native[0].session() as session:
        assert session.one("SELECT state,billed_micro_usd FROM model_usage") == {
            "state": "UNKNOWN", "billed_micro_usd": 2**40}
        assert session.one("SELECT vector FROM memory_vectors") == {"vector": b"\x00\xff\x80"}
        assert session.one("SELECT state,attempt_count,delivery_key FROM outbox") == {
            "state": "DELIVERY_UNCERTAIN", "attempt_count": 1,
            "delivery_key": "native-original-key"}
        assert audit(session, "{}")["audit_id"] == 2**40 + 1
    with pytest.raises(RuntimeError, match="SQLITE_IMPORT_TARGET_NOT_EMPTY"):
        import_sqlite_backup(source, empty_native[1])


def test_reconciliation_failure_rolls_back_all_imported_tables(empty_native, tmp_path, monkeypatch):
    from companion import legacy_import

    source = tmp_path / "stopped-backup.sqlite3"
    legacy_backup(source)

    def damaged_comparison(*_):
        raise RuntimeError("synthetic reconciliation failure")

    monkeypatch.setattr(legacy_import, "_compare_table", damaged_comparison)
    with pytest.raises(RuntimeError, match="synthetic reconciliation failure"):
        import_sqlite_backup(source, empty_native[1])
    with empty_native[0].session() as session:
        for name in ["model_usage", "memories", "memory_vectors", "audit_log", "runtime_meta"]:
            assert session.one(f"SELECT count(*) AS n FROM {name}") == {"n": 0}
