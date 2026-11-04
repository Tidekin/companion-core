"""Upgrade populated historical schemas, without downgrading a current database."""

import hashlib
import re
import struct
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet

from companion import cloud_budget
from companion.backup import backup, restore
from companion.config import Settings
from companion.core import Core
from companion.privacy import ErasureLedger
from companion.store import Store, encode, migrate
from companion.transport import digest
from tests.conftest import Clock
from tests.test_test_fixtures import historical_database

REVISION_CHAIN = (
    "0001_m0",
    "0002_m1",
    "0003_memory_index",
    "0004_diary",
    "0005_observations",
    "0006_affect",
    "0007_consolidation",
    "0008_goals",
    "0009_exploration",
    "0011_executive",
    "0012_media",
    "0013_content",
    "0014_reader_budget",
    "0015_article_images",
    "0016_source_grants",
    "0017_paid_model_gate",
    "0018_reflection_goal_lineage",
    "0019_segmented_replies",
    "0020_reply_segment_capacity",
    "0021_reflection_admission_retry",
    "0022_input_reply_revision",
    "0023_meme_discovery_reservations",
    "0024_reading_attempts",
    "0025_meme_captions",
    "0026_cloud_budget",
    "0027_owner_console",
    "0028_private_files",
    "0029_social_autonomy",
    "0030_private_poke",
    "0031_voice_nodes",
    "0032_bili_video",
    "0033_model_usage_job_index",
    "0034_voice_retention_v2",
    "0035_token_measurement",
    "0036_social_plan_jobs",
    "0037_life_topics",
    "0038_life_goal_binding",
    "0039_evaluation",
    "0040_owner_authority",
    "0041_agent_conversations",
    "0042_rem",
    "0043_reader_authority",
    "0044_voice_owner_authority",
    "0045_goal_read_plan",
    "0046_capability_delegation",
    "0047_meme_assets",
    "0048_meme_vectors",
    "0049_evaluation_ordinals",
    "0050_watch_ledger",
    "0051_exploration_query",
    "0052_style_distill",
    "0053_owner_jargon",
    "0054_life_autonomy_projection",
    "0055_runtime_configuration",
    "0056_watch_ledger_goal_reads",
    "0057_expression_configuration",
    "0058_bili_discovery",
    "0059_gateway",
    "0060_mood_appraisals",
    "0061_singing_voice",
    "0062_generated_images",
    "0063_mood_appraisal_targets",
    "0064_life_topic_items",
)
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
SEGMENT_DEFAULTS = {"reply_group_id": None, "segment_index": 0, "segment_count": 1,
                    "reply_group_sha256": None, "segment_interval_ms": 0}


def table_snapshot(store, tables):
    """Capture every existing column, including opaque vector bytes and provenance IDs."""
    return {
        table: {
            "columns": [row["name"] for row in store.rows(f'PRAGMA table_info("{table}")')],
            "rows": store.rows(f'SELECT * FROM "{table}" ORDER BY rowid'),
        }
        for table in tables
    }


def assert_old_columns_unchanged(store, snapshot):
    for table, before in snapshot.items():
        columns = ",".join('"' + column + '"' for column in before["columns"])
        assert store.rows(f'SELECT {columns} FROM "{table}" ORDER BY rowid') == before["rows"], (
            table
        )


def assert_indexes_and_integrity(store, surviving=("erase", "keep")):
    assert store.one("PRAGMA integrity_check")["integrity_check"] == "ok"
    assert store.rows("PRAGMA foreign_key_check") == []
    for name, token in TOKENS.items():
        expected = [{"memory_id": "mem_" + name}] if name in surviving else []
        assert (
            store.rows("SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ?", (token,))
            == expected
        )
    assert {
        row["name"] for row in store.rows("SELECT name FROM sqlite_master WHERE type='trigger'")
    } >= {"memory_fts_insert", "memory_fts_update", "memory_fts_delete", "memory_vector_invalidate"}


def seed_legacy(tmp_path, revision):
    database, ledger_path = tmp_path / "historical.db", tmp_path / "erasure.jsonl"
    migrate(str(database), revision)
    with closing(Store(str(database))) as store:
        assert store.one("SELECT version_num FROM alembic_version")["version_num"] == revision
        assert store.one("SELECT 1 FROM sqlite_master WHERE name='observations'") is None
        ErasureLedger(ledger_path).initialize(store)
        with store.transaction():
            for actor in ("owner", "companion", "system"):
                store.insert("principals", actor_id=actor, kind=actor, created_at=TIMES["erase"])
            store.execute("INSERT INTO runtime_meta VALUES ('owner_qq_id','10001')")
            store.execute("INSERT INTO runtime_meta VALUES ('privacy_epoch','3')")
            store.insert(
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
                store.insert(
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
                store.insert(
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
                store.insert(
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
                store.insert(
                    "memory_evidence",
                    memory_id="mem_" + name,
                    event_id="evt_" + name,
                    polarity=1,
                    created_at=now,
                )
                store.insert(
                    "memory_revisions",
                    revision_id="revision_" + name,
                    memory_id="mem_" + name,
                    revision_reason="owner_confirmation",
                    prior_status="HYPOTHESIS",
                    prior_content="可能喜欢" + token,
                    changed_by="owner",
                    created_at=now,
                )
                store.insert(
                    "memory_vectors",
                    memory_id="mem_" + name,
                    model_version="synthetic-legacy-index-v1",
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    vector=struct.pack("<4f", 1.0, 0.25, 0.5, 0.75 if name == "keep" else -0.75),
                )
            for state in DELIVERY_STATES:
                body = {"text": "历史投递记录 " + state}
                store.insert(
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
                store.insert(
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
        store.insert(
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
        store.insert(
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
        store.insert(
            "diary_evidence",
            entry_id="diary_" + name,
            source_id="event:evt_" + name,
            event_id="evt_" + name,
            excerpt_sha256=hashlib.sha256(token.encode()).hexdigest(),
        )
        store.insert(
            "diary_revisions",
            revision_id="diary_revision_" + name,
            entry_id="diary_" + name,
            prior_title="最初标题 " + day,
            prior_body_md="最初的" + token + "记录",
            reason="owner_correction",
            changed_by="owner",
            created_at=now,
        )


def settings_for(database, ledger):
    return Settings(
        database=str(database),
        erasure_ledger=str(ledger),
        owner_qq_id="10001",
        bot_qq_id="10002",
        bridge_token="migration-test-token-" * 2,
        memory_enabled=False,
        proactive_enabled=False,
    )


def test_alembic_has_one_real_head_and_expected_legacy_upgrade_chain():
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [REVISION_CHAIN[-1]]
    assert scripts.get_bases() == ["0001_m0"]
    revisions, current = [], scripts.get_revision(REVISION_CHAIN[-1])
    while current:
        revisions.append(current.revision)
        assert current.down_revision is None or isinstance(current.down_revision, str)
        current = scripts.get_revision(current.down_revision) if current.down_revision else None
    assert tuple(reversed(revisions)) == REVISION_CHAIN


@pytest.mark.parametrize("revision,code", [
    ("0040_owner_authority", "OWNER_AUTHORITY_DOWNGRADE"),
    ("0043_reader_authority", "OWNER_AUTHORITY_DOWNGRADE"),
    ("0044_voice_owner_authority", "VOICE_AUTHORITY_DOWNGRADE"),
    ("0045_goal_read_plan", "GOAL_PLAN_DOWNGRADE"),
    ("0061_singing_voice", "SINGING_DOWNGRADE"),
])
def test_authority_downgrade_keeps_custody_and_accounting_unchanged(core, tmp_path, revision, code):
    tables = ("runtime_meta", "voice_jobs", "voice_node_state", "model_usage", "outbox")
    path = historical_database(core.store, tmp_path / "authority-custody.db", revision)
    with closing(Store(path)) as original:
        before = table_snapshot(original, tables)
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    config.set_main_option("sqlalchemy.url", "sqlite:///" + path.as_posix())
    with pytest.raises(RuntimeError, match=code + "_REQUIRES_EXPLICIT_CUSTODY_RECONCILIATION"):
        command.downgrade(config, "0033_model_usage_job_index")
    with closing(Store(path)) as original:
        assert table_snapshot(original, tables) == before
        assert original.one("SELECT version_num FROM alembic_version")["version_num"] == revision


@pytest.mark.parametrize("revision", ["0003_memory_index", "0004_diary"])
def test_populated_historical_upgrade_preserves_every_old_column_and_repeats(tmp_path, revision):
    database, _, before = seed_legacy(tmp_path, revision)
    migrate(str(database), "0014_reader_budget")
    with closing(Store(str(database))) as store:
        assert store.rows("SELECT version_num FROM alembic_version") == [
            {"version_num": "0014_reader_budget"}
        ]
        assert_old_columns_unchanged(store, before)
        assert_indexes_and_integrity(store)
        assert not store.schema_capabilities()
        first_upgrade = table_snapshot(store, before)
    migrate(str(database), "head")
    with closing(Store(str(database))) as store:
        incarnation = store.one("SELECT * FROM runtime_meta WHERE key='voice_incarnation'")
        assert incarnation and re.fullmatch(r"[a-f0-9]{32}", incarnation["value"])
        first_upgrade["runtime_meta"]["rows"].append(incarnation)
        first_upgrade["runtime_meta"]["rows"].extend([
            {"key": "owner_authority_restore_generation", "value": "initial"},
            {"key": "owner_authority_mode", "value": "OFF"},
        ])
        assert_old_columns_unchanged(store, first_upgrade)
        for row in store.rows("SELECT * FROM outbox"):
            assert {key: row[key] for key in SEGMENT_DEFAULTS} == SEGMENT_DEFAULTS
        final_upgrade = table_snapshot(store, before)
        assert_indexes_and_integrity(store)
        assert store.schema_capabilities()
    migrate(str(database), "head")
    with closing(Store(str(database))) as store:
        assert table_snapshot(store, before) == final_upgrade


@pytest.mark.parametrize("forget_one", [False, True])
def test_encrypted_0003_backup_restores_with_newest_ledger_and_preserves_independent_fact(
    tmp_path, forget_one
):
    database, ledger, before = seed_legacy(tmp_path, "0003_memory_index")
    archive, destination, key = (
        tmp_path / "old-0003.enc",
        tmp_path / "restored.db",
        Fernet.generate_key(),
    )
    backup(database, archive, key)
    ciphertext = archive.read_bytes()
    assert not ciphertext.startswith(b"SQLite") and TOKENS["keep"].encode() not in ciphertext
    assert Fernet(key).decrypt(ciphertext).startswith(b"SQLite format 3")
    migrate(str(database))
    if forget_one:
        with closing(Store(str(database))) as current, current.transaction():
            ErasureLedger(ledger).append(current, "mem_erase", Clock()())
    restore(archive, destination, key, ledger)
    with closing(Store(str(destination))) as restored:
        generation = restored.one(
            "SELECT * FROM runtime_meta WHERE key='memory_control_restore_generation'"
        )
        assert generation and re.fullmatch(r"restore_[a-f0-9]{32}", generation["value"])
        restore_block = restored.one(
            "SELECT * FROM runtime_meta WHERE key='cloud_budget_restore_required'"
        )
        assert restore_block == {"key": "cloud_budget_restore_required", "value": "1"}
        assert restored.rows("SELECT version_num FROM alembic_version") == [
            {"version_num": REVISION_CHAIN[-1]}
        ]
        voice_incarnation = restored.one(
            "SELECT * FROM runtime_meta WHERE key='voice_incarnation'"
        )
        assert voice_incarnation and re.fullmatch(r"[a-f0-9]{32}", voice_incarnation["value"])
        voice_restore = restored.one(
            "SELECT * FROM runtime_meta WHERE key='voice_restore_required'"
        )
        assert voice_restore == {"key": "voice_restore_required", "value": "1"}
        assert restored.schema_capabilities()
        assert_indexes_and_integrity(restored, ("keep",) if forget_one else ("erase", "keep"))
        for table in ("memories", "memory_evidence", "memory_revisions", "memory_vectors"):
            expected = [row for row in before[table]["rows"] if row["memory_id"] == "mem_keep"]
            assert restored.rows(f"SELECT * FROM {table} WHERE memory_id='mem_keep'") == expected
        assert restored.one("SELECT * FROM events WHERE event_id='evt_keep'") == {
            **next(row for row in before["events"]["rows"] if row["event_id"] == "evt_keep"),
            "reply_revision": 0,
        }
        for usage in before["model_usage"]["rows"]:
            expected = {
                **usage,
                "state": "UNKNOWN" if usage["state"] == "RESERVED" else usage["state"],
            }
            assert (
                restored.one("SELECT * FROM model_usage WHERE request_id=?", (usage["request_id"],))
                == expected
            )
        for old in before["outbox"]["rows"]:
            actual = restored.one("SELECT * FROM outbox WHERE message_id=?", (old["message_id"],))
            active = old["state"] in {"PENDING", "LEASED", "SENDING", "RETRY_WAIT"}
            if forget_one:
                expected_state = (
                    "CANCELLED"
                    if old["state"] in {"PENDING", "LEASED", "RETRY_WAIT"}
                    else "DELIVERY_UNCERTAIN"
                    if old["state"] == "SENDING"
                    else old["state"]
                )
                assert actual["state"] == expected_state
                assert actual["content_json"] == "{}" and actual["content_sha256"] == ""
            else:
                expected = {
                    **old, **SEGMENT_DEFAULTS,
                    "delivery_lane": "owner_private", "native_ack_json": None,
                    "authority_json": "{}",
                }
                if active:
                    expected.update(
                        state="DELIVERY_UNCERTAIN",
                        lease_token=None,
                        last_error_code="RESTORED_DELIVERY_REQUIRES_RECONCILIATION",
                    )
                assert actual == expected
        if forget_one:
            assert restored.one(
                "SELECT content,status FROM memories WHERE memory_id='mem_erase'"
            ) == {"content": "", "status": "FORGOTTEN"}
            assert restored.one(
                "SELECT body_json,redacted FROM events WHERE event_id='evt_erase'"
            ) == {"body_json": "{}", "redacted": 1}
            for table in ("memory_evidence", "memory_revisions", "memory_vectors"):
                assert restored.rows(f"SELECT * FROM {table} WHERE memory_id='mem_erase'") == []
            assert restored.one(
                "SELECT restore_min_replay_version,state FROM privacy_erasure_ledger"
            ) == {"restore_min_replay_version": 1, "state": "COMPLETE"}
        else:
            unchanged = {
                table: rows
                for table, rows in before.items()
                if table not in {"outbox", "model_usage"}
            }
            unchanged["runtime_meta"] = {
                **before["runtime_meta"],
                "rows": before["runtime_meta"]["rows"] + [voice_incarnation] + [
                    restored.one("SELECT * FROM runtime_meta WHERE key=?", (name,))
                    for name in ("owner_authority_restore_generation", "owner_authority_mode")
                ] + [generation, restore_block] + [
                    restored.one("SELECT * FROM runtime_meta WHERE key=?", (name,))
                    for name in ("social_autonomy_restore_required", "voice_restore_required",
                                 "bili_video_restore_required")
                ],
            }
            assert_old_columns_unchanged(restored, unchanged)


def test_encrypted_0004_diary_restore_deletes_only_requested_date(tmp_path):
    database, ledger, before = seed_legacy(tmp_path, "0004_diary")
    archive, destination, key = (
        tmp_path / "old-0004.enc",
        tmp_path / "restored-diary.db",
        Fernet.generate_key(),
    )
    backup(database, archive, key)
    migrate(str(database))
    instance = Core(settings_for(database, ledger), clock=Clock())
    try:
        instance.diary.forget(date.fromisoformat(TIMES["erase"][:10]), "10001")
    finally:
        instance.store.close()
    restore(archive, destination, key, ledger)
    with closing(Store(str(destination))) as restored:
        assert restored.one(
            "SELECT status,body_md FROM diary_entries WHERE entry_id='diary_erase'"
        ) == {"status": "DELETED", "body_md": ""}
        for table in ("diary_evidence", "diary_revisions"):
            assert restored.rows(f"SELECT * FROM {table} WHERE entry_id='diary_erase'") == []
        for table in DIARY_TABLES:
            key_column = "run_id" if table == "diary_runs" else "entry_id"
            value = "diary_run_keep" if table == "diary_runs" else "diary_keep"
            assert restored.rows(f"SELECT * FROM {table} WHERE {key_column}=?", (value,)) == [
                row for row in before[table]["rows"] if row[key_column] == value
            ]
        assert_old_columns_unchanged(
            restored,
            {
                table: before[table]
                for table in ("memories", "memory_evidence", "memory_revisions", "memory_vectors")
            },
        )
        assert_indexes_and_integrity(restored)


def test_0018_preserves_populated_goals_without_guessing_reflection_origins(tmp_path):
    database, _, _ = seed_legacy(tmp_path, "0004_diary")
    migrate(str(database), "0017_paid_model_gate")
    with closing(Store(str(database))) as store:
        store.insert(
            "reflection_runs", run_id="reflection_legacy", identity_id="companion",
            local_date="2026-10-18", timezone="Asia/Shanghai", state="COMMITTED",
            source_set_sha256="a" * 64, captured_privacy_epoch=3,
            created_at=TIMES["keep"], updated_at=TIMES["keep"],
        )
        store.insert(
            "reflection_suggestions", suggestion_id="suggestion_legacy", run_id="reflection_legacy",
            kind="next_question", content="同样的历史主题", evidence_json="[]", state="PENDING",
            created_at=TIMES["keep"], updated_at=TIMES["keep"],
        )
        store.insert(
            "goals", goal_id="goal_legacy", kind="self", topic="同样的历史主题",
            scope_json=encode({"operation": "investigate", "url": ""}), proposal_key="legacy-key",
            state="CANDIDATE", priority=1, next_due_at=TIMES["keep"],
            created_at=TIMES["keep"], updated_at=TIMES["keep"],
        )
        store.insert("goal_evidence", goal_id="goal_legacy", event_id="evt_keep")
        before = table_snapshot(store, (
            "goals", "goal_evidence", "reflection_runs", "reflection_suggestions",
            "events", "memories", "model_usage", "outbox", "runtime_meta",
        ))
    # Exercise this historical migration's own downgrade guard, before later
    # irreversible custody migrations introduce their independent stop boundary.
    migrate(str(database), "0018_reflection_goal_lineage")
    migrate(str(database), "0018_reflection_goal_lineage")
    with closing(Store(str(database))) as store:
        assert_old_columns_unchanged(store, before)
        assert store.one("SELECT reflection_suggestion_id FROM goals") == {
            "reflection_suggestion_id": None
        }
        assert store.rows("PRAGMA foreign_key_check") == []
        # An explicit attribution is retained; downgrading may not silently discard it.
        store.execute(
            "UPDATE goals SET reflection_suggestion_id='suggestion_legacy' "
            "WHERE goal_id='goal_legacy'"
        )
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    config.set_main_option("sqlalchemy.url", "sqlite:///" + database.resolve().as_posix())
    with pytest.raises(RuntimeError, match="REFLECTION_GOALS_DOWNGRADE_REQUIRES_EXPLICIT_REVIEW"):
        command.downgrade(config, "0017_paid_model_gate")
    with closing(Store(str(database))) as store:
        assert store.one("SELECT reflection_suggestion_id FROM goals") == {
            "reflection_suggestion_id": "suggestion_legacy"
        }
        assert_old_columns_unchanged(store, before)
        assert store.one("PRAGMA integrity_check")["integrity_check"] == "ok"


def test_0021_retains_reflection_provenance_and_guards_pending_wait_downgrade(tmp_path):
    database, _, _ = seed_legacy(tmp_path, "0004_diary")
    migrate(str(database), "0020_reply_segment_capacity")
    tables = ("reflection_runs", "reflection_evidence", "reflection_suggestions", "model_usage")
    with closing(Store(str(database))) as store:
        for index, state in enumerate(("FAILED", "COMMITTED", "SKIPPED", "CANCELLED")):
            run_id = "old_reflection_" + str(index)
            store.insert(
                "reflection_runs", run_id=run_id, identity_id="companion",
                local_date=f"2026-10-{10 + index}", timezone="Asia/Shanghai", state=state,
                source_set_sha256="a" * 64, captured_privacy_epoch=2,
                last_error_code="SCENARIO_CALL_LIMIT" if state == "FAILED" else None,
                created_at=TIMES["keep"], updated_at=TIMES["keep"],
            )
            store.insert("reflection_evidence", run_id=run_id, event_id="evt_keep",
                         source_id="owner:evt_keep")
        store.insert(
            "reflection_suggestions", suggestion_id="old_suggestion", run_id="old_reflection_1",
            kind="next_question", content="继续了解银杉航线", evidence_json="[]", state="PENDING",
            created_at=TIMES["keep"], updated_at=TIMES["keep"],
        )
        before = table_snapshot(store, tables)
    migrate(str(database), "0021_reflection_admission_retry")
    migrate(str(database), "0021_reflection_admission_retry")
    with closing(Store(str(database))) as store:
        assert_old_columns_unchanged(store, before)
        assert all(row["next_attempt_at"] is None
                   for row in store.rows("SELECT * FROM reflection_runs"))
        assert store.rows("PRAGMA foreign_key_check") == []
        store.execute("UPDATE reflection_runs SET state='RETRY_WAIT',next_attempt_at=? "
                      "WHERE run_id='old_reflection_0'", (TIMES["keep"],))
        pending = table_snapshot(store, tables)
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    config.set_main_option("sqlalchemy.url", "sqlite:///" + database.resolve().as_posix())
    with pytest.raises(
        RuntimeError, match="REFLECTION_RETRY_DOWNGRADE_REQUIRES_EXPLICIT_DATA_REVIEW"
    ):
        command.downgrade(config, "0020_reply_segment_capacity")
    with closing(Store(str(database))) as store:
        assert table_snapshot(store, tables) == pending
        store.execute("UPDATE reflection_runs SET state='FAILED',next_attempt_at=NULL "
                      "WHERE run_id='old_reflection_0'")
    command.downgrade(config, "0020_reply_segment_capacity")
    with closing(Store(str(database))) as store:
        assert table_snapshot(store, tables) == before
        assert store.rows("PRAGMA foreign_key_check") == []


def test_0033_job_index_preserves_rows_rowids_budget_snapshots_and_erasure_ledger(tmp_path):
    database, ledger, _ = seed_legacy(tmp_path, "0004_diary")
    migrate(str(database), "0032_bili_video")
    with closing(Store(str(database))) as store:
        original = store.one("SELECT * FROM model_usage WHERE state='UNKNOWN'")
        # A gap makes accidental table rebuilds visible, including the NULL-job case.
        store.insert("model_usage", rowid=41, **{**original, "usage_id": "usage_null_job",
                     "request_id": "request_null_job", "job_id": None})
        store.insert("model_budget_windows", window_id="migration-window",
                     contract_sha256="a" * 64)
        for index, row in enumerate(store.rows("SELECT * FROM model_usage ORDER BY rowid")):
            store.insert("model_admissions", request_id=row["request_id"],
                         logical_request_id="job_keep", attempt_id=f"attempt_{index}",
                         attempt_number=index + 1, window_id="migration-window",
                         contract_sha256="a" * 64, payload_sha256="b" * 64,
                         snapshot_json=encode({"historical_pricing": True}),
                         reserved_micro_usd=row["billed_micro_usd"], input_bound=1000,
                         output_bound=100, lease_token=f"lease_{index}",
                         lease_until=TIMES["keep"], io_started_at=TIMES["keep"],
                         journal_path=str(tmp_path / "retained-budget.jsonl"),
                         journal_id="retained-budget")
        cloud_budget.policy(store, settings_for(database, ledger), Clock()())
        assert store.one("SELECT COUNT(*) n FROM cloud_cost_snapshots")["n"] == 4
        tables = store.rows(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name<>'alembic_version' "
            "ORDER BY name",
        )
        schema = store.rows("SELECT type,name,tbl_name,sql FROM sqlite_master "
                            "WHERE name<>'alembic_version' ORDER BY name")

        def rows_with_rowids(current):
            snapshot = {}
            for table in tables:
                name = table["name"]
                if "WITHOUT ROWID" in " ".join(table["sql"].upper().split()):
                    primary = sorted((row for row in current.rows(f'PRAGMA table_info("{name}")')
                                      if row["pk"]), key=lambda row: row["pk"])
                    assert primary
                    order = ",".join('"' + row["name"] + '"' for row in primary)
                    query = f'SELECT * FROM "{name}" ORDER BY {order}'
                else:
                    query = f'SELECT rowid AS snapshot_rowid,* FROM "{name}" ORDER BY rowid'
                snapshot[name] = current.rows(query)
            return snapshot

        before = rows_with_rowids(store)
    ledger_bytes = ledger.read_bytes()
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    config.set_main_option("sqlalchemy.url", "sqlite:///" + database.as_posix())
    for operation in ("upgrade", "upgrade", "downgrade", "upgrade"):
        if operation == "upgrade":
            migrate(str(database), "0033_model_usage_job_index")
        else:
            command.downgrade(config, "0032_bili_video")
        with closing(Store(str(database))) as store:
            assert rows_with_rowids(store) == before
            assert ledger.read_bytes() == ledger_bytes
            assert store.rows("SELECT type,name,tbl_name,sql FROM sqlite_master "
                              "WHERE name NOT IN ('alembic_version','idx_model_usage_job') "
                              "ORDER BY name") == schema
            assert store.rows("PRAGMA foreign_key_check") == []
            assert store.one("PRAGMA integrity_check") == {"integrity_check": "ok"}
            assert store.one("SELECT version_num FROM alembic_version")["version_num"] == (
                "0033_model_usage_job_index" if operation == "upgrade" else "0032_bili_video"
            )
            indexes = store.rows("PRAGMA index_list(model_usage)")
            actual = [row for row in indexes if row["name"] == "idx_model_usage_job"]
            if operation == "upgrade":
                assert len(actual) == 1 and actual[0]["unique"] == 0
                assert [row["name"] for row in store.rows(
                    "PRAGMA index_info(idx_model_usage_job)")] == ["job_id"]
                plan = store.rows("EXPLAIN QUERY PLAN SELECT request_id FROM model_usage "
                                  "WHERE job_id=?", ("job_keep",))
                assert any("idx_model_usage_job" in row["detail"] for row in plan)
            else:
                assert actual == []
