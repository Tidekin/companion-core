import hashlib
import sqlite3
import struct
from contextlib import closing

from companion.legacy_import import migrate_sqlite
from companion.privacy import ErasureLedger
from companion.store import encode, uid
from companion.transport import digest

LEGACY_TABLES = (
    "principals",
    "conversations",
    "events",
    "jobs",
    "memories",
    "memory_evidence",
    "memory_revisions",
    "memories_fts",
    "memory_vectors",
    "outbox",
    "model_usage",
    "runtime_meta",
)


DIARY_TABLES = ("diary_runs", "diary_entries", "diary_evidence", "diary_revisions")


TOKENS = {"erase": "菠萝航线", "keep": "银杉航线"}


TIMES = {"erase": "2026-10-17T04:00:00.000000+00:00", "keep": "2026-10-18T04:00:00.000000+00:00"}


DELIVERY_STATES = ("SENT", "PENDING", "LEASED", "SENDING", "RETRY_WAIT", "DELIVERY_UNCERTAIN")


def open_historical(path):
    """Open a real offline SQLite fixture, never the running Core storage API."""
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = lambda cursor, row: dict(
        zip((item[0] for item in cursor.description), row, strict=True)
    )
    db.execute("PRAGMA foreign_keys=ON")
    return db


def rows(db, statement, parameters=()):
    return db.execute(statement, parameters).fetchall()


def one(db, statement, parameters=()):
    return db.execute(statement, parameters).fetchone()


def insert(db, table, **values):
    columns = ",".join('"' + name + '"' for name in values)
    db.execute(
        f'INSERT INTO "{table}" ({columns}) VALUES ({",".join("?" for _ in values)})',
        tuple(values.values()),
    )


def table_snapshot(store, tables):
    """Capture every existing column, including opaque vector bytes and provenance IDs."""
    return {
        table: {
            "columns": [row["name"] for row in rows(store, f'PRAGMA table_info("{table}")')],
            "rows": rows(store, f'SELECT * FROM "{table}" ORDER BY rowid'),
        }
        for table in tables
    }


def assert_old_columns_unchanged(store, snapshot):
    for table, before in snapshot.items():
        columns = ",".join('"' + column + '"' for column in before["columns"])
        assert rows(store, f'SELECT {columns} FROM "{table}" ORDER BY rowid') == before["rows"], (
            table
        )


def assert_indexes_and_integrity(store, surviving=("erase", "keep")):
    assert one(store, "PRAGMA integrity_check")["integrity_check"] == "ok"
    assert rows(store, "PRAGMA foreign_key_check") == []
    for name, token in TOKENS.items():
        expected = [{"memory_id": "mem_" + name}] if name in surviving else []
        assert (
            rows(store, "SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ?", (token,))
            == expected
        )
    assert {
        row["name"] for row in rows(store, "SELECT name FROM sqlite_master WHERE type='trigger'")
    } >= {"memory_fts_insert", "memory_fts_update", "memory_fts_delete", "memory_vector_invalidate"}


def assert_native_indexes_and_integrity(store, surviving=("erase", "keep")):
    assert store.schema_capabilities()
    assert not store.one(
        "SELECT 1 FROM pg_constraint WHERE connamespace=current_schema()::regnamespace "
        "AND NOT convalidated LIMIT 1"
    )
    for name, token in TOKENS.items():
        expected = [{"memory_id": "mem_" + name}] if name in surviving else []
        assert (
            store.rows(
                "SELECT memory_id FROM memories WHERE content ILIKE %s AND status='CONFIRMED'",
                ("%" + token + "%",),
            )
            == expected
        )


def assert_native_old_columns_unchanged(store, snapshot):
    for table, before in snapshot.items():
        if table == "memories_fts":
            continue  # Native text retrieval indexes the authoritative memories table.
        columns = ",".join('"' + column + '"' for column in before["columns"])
        actual = store.rows(f'SELECT {columns} FROM "{table}" ORDER BY record_sequence')
        expected = before["rows"]
        if table == "runtime_meta":
            # Native restore replaces custody markers; their insertion positions change.
            actual = sorted(actual, key=lambda row: row["key"])
            expected = sorted(expected, key=lambda row: row["key"])
        assert actual == expected, table


def seed_legacy(tmp_path, revision):
    database, ledger_path = tmp_path / "historical.db", tmp_path / "erasure.jsonl"
    migrate_sqlite(database, revision)
    with closing(open_historical(database)) as store:
        assert one(store, "SELECT version_num FROM alembic_version")["version_num"] == revision
        assert one(store, "SELECT 1 FROM sqlite_master WHERE name='observations'") is None
        header = {"ledger_id": uid("ledger")}
        ledger_path.write_text(encode(header) + "\n", encoding="utf-8")
        store.execute("INSERT INTO runtime_meta VALUES ('ledger_id', ?)", (header["ledger_id"],))
        assert ErasureLedger(ledger_path).read() == [header]
        store.execute("BEGIN")
        with store:
            for actor in ("owner", "companion", "system"):
                insert(store, "principals", actor_id=actor, kind=actor, created_at=TIMES["erase"])
            store.execute("INSERT INTO runtime_meta VALUES ('owner_qq_id','10001')")
            store.execute("INSERT INTO runtime_meta VALUES ('privacy_epoch','3')")
            insert(
                store,
                "conversations",
                conversation_id="conv_legacy",
                platform="qq.onebot",
                platform_session_key="qq:FriendMessage:10001",
                scope="private",
                owner_actor_id="owner",
                last_event_at=TIMES["keep"],
                created_at=TIMES["erase"],
            )
            for name, token in TOKENS.items():
                now, text = TIMES[name], "我计划阅读" + token + "系列小说。"
                insert(
                    store,
                    "events",
                    event_id="evt_" + name,
                    schema_version=1,
                    source="qq.onebot",
                    source_event_id="legacy-qq-" + name,
                    conversation_id="conv_legacy",
                    actor_id="owner",
                    event_type="message.received",
                    occurred_at=now,
                    ingested_at=now,
                    privacy_scope="private",
                    body_json=encode({"text": text, "media": []}),
                    trace_id="trace_" + name,
                    idempotency_key="inbox_" + name,
                    fingerprint=digest({"text": text}),
                    memory_processed=1,
                )
                insert(
                    store,
                    "jobs",
                    job_id="job_" + name,
                    event_id="evt_" + name,
                    kind="chat",
                    state="SUCCEEDED",
                    idempotency_key="job_key_" + name,
                    attempts=1,
                    max_attempts=3,
                    created_at=now,
                    updated_at=now,
                )
                insert(
                    store,
                    "memories",
                    memory_id="mem_" + name,
                    subject="user",
                    kind="preference",
                    content=text,
                    status="CONFIRMED",
                    origin="user_confirmed",
                    confidence=1.0,
                    importance=0.7,
                    privacy_scope="private",
                    fact_key="reading_" + name,
                    created_at=now,
                    updated_at=now,
                    valid_from=now,
                    last_reviewed_at=now,
                )
                insert(
                    store,
                    "memory_evidence",
                    memory_id="mem_" + name,
                    event_id="evt_" + name,
                    polarity=1,
                    created_at=now,
                )
                insert(
                    store,
                    "memory_revisions",
                    revision_id="revision_" + name,
                    memory_id="mem_" + name,
                    revision_reason="owner_confirmation",
                    prior_status="HYPOTHESIS",
                    prior_content="可能喜欢" + token,
                    changed_by="owner",
                    created_at=now,
                )
                insert(
                    store,
                    "memory_vectors",
                    memory_id="mem_" + name,
                    model_version="synthetic-legacy-index-v1",
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    vector=struct.pack("<4f", 1.0, 0.25, 0.5, 0.75 if name == "keep" else -0.75),
                )
            for state in DELIVERY_STATES:
                body = {"text": "历史投递记录 " + state}
                insert(
                    store,
                    "outbox",
                    message_id="msg_" + state,
                    origin_event_id="evt_keep",
                    conversation_id="conv_legacy",
                    action_type="qq.private.reply",
                    content_json=encode(body),
                    content_sha256=digest(body),
                    state=state,
                    delivery_key="delivery_" + state,
                    lease_owner="historical-bridge" if state in {"LEASED", "SENDING"} else None,
                    lease_token="token_" + state if state in {"LEASED", "SENDING"} else None,
                    lease_until=TIMES["keep"] if state in {"LEASED", "SENDING"} else None,
                    attempt_count=1,
                    provider_message_id="qq-123" if state == "SENT" else None,
                    created_at=TIMES["keep"],
                    updated_at=TIMES["keep"],
                    sent_at=TIMES["keep"] if state == "SENT" else None,
                )
            for index, state in enumerate(("SETTLED", "RESERVED", "UNKNOWN"), 1):
                insert(
                    store,
                    "model_usage",
                    usage_id="usage_" + state,
                    request_id="request_" + state,
                    job_id="job_keep",
                    scenario="chat",
                    provider="historical-fixture",
                    model="fixture-model",
                    input_tokens=index * 100,
                    cached_input_tokens=index * 10,
                    output_tokens=index * 20,
                    billed_micro_usd=index * 1234,
                    is_estimate=int(state != "SETTLED"),
                    occurred_at=TIMES["keep"],
                    state=state,
                    pricing_known=1,
                )
            if revision == "0004_diary":
                seed_legacy_diaries(store)
        tables = LEGACY_TABLES + (DIARY_TABLES if revision == "0004_diary" else ())
        snapshot = table_snapshot(store, tables)
        assert all(snapshot[table]["rows"] for table in tables)
        assert_indexes_and_integrity(store)
    return database, ledger_path, snapshot


def seed_legacy_diaries(store):
    for name, token in TOKENS.items():
        now, day = TIMES[name], TIMES[name][:10]
        insert(
            store,
            "diary_runs",
            run_id="diary_run_" + name,
            identity_id="companion",
            local_date=day,
            timezone="Asia/Shanghai",
            state="COMMITTED",
            source_set_sha256="source_hash_" + name,
            captured_privacy_epoch=3,
            attempts=1,
            max_attempts=2,
            created_at=now,
            updated_at=now,
        )
        insert(
            store,
            "diary_entries",
            entry_id="diary_" + name,
            run_id="diary_run_" + name,
            identity_id="companion",
            local_date=day,
            timezone="Asia/Shanghai",
            title=day + " 的阅读计划",
            body_md="主人说：今天讨论了" + token,
            status="COMMITTED",
            source_set_sha256="source_hash_" + name,
            captured_privacy_epoch=3,
            generator_version="diary-1",
            model_name="historical-diary-model",
            version=2,
            created_at=now,
            updated_at=now,
        )
        insert(
            store,
            "diary_evidence",
            entry_id="diary_" + name,
            source_id="event:evt_" + name,
            event_id="evt_" + name,
            excerpt_sha256=hashlib.sha256(token.encode()).hexdigest(),
        )
        insert(
            store,
            "diary_revisions",
            revision_id="diary_revision_" + name,
            entry_id="diary_" + name,
            prior_title="最初标题 " + day,
            prior_body_md="最初的" + token + "记录",
            reason="owner_correction",
            changed_by="owner",
            created_at=now,
        )
