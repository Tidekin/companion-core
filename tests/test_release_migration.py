"""Upgrade populated historical schemas, without downgrading a current database."""

import re
from contextlib import closing, contextmanager
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
from companion.erasure import CONTRACT_KEY, contract_signature
from companion.legacy_import import TARGET_REVISION, import_sqlite_backup, migrate_sqlite
from companion.postgres import Database
from companion.privacy import ErasureLedger
from companion.store import Store, encode
from tests.conftest import Clock
from tests.postgres_fixture import isolated_database
from tests.support.historical_schema import historical_database
from tests.support.release_migration import (
    DIARY_TABLES,
    TIMES,
    TOKENS,
    assert_indexes_and_integrity,
    assert_native_indexes_and_integrity,
    assert_native_old_columns_unchanged,
    assert_old_columns_unchanged,
    insert,
    one,
    open_historical,
    rows,
    seed_legacy,
    table_snapshot,
)

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
    "0065_bili_grant_expiry",
    "0066_chat_retrieval_bounds",
    "0067_relationship_standing",
    "0068_image_review_target",
)
SEGMENT_DEFAULTS = {
    "reply_group_id": None,
    "segment_index": 0,
    "segment_count": 1,
    "reply_group_sha256": None,
    "segment_interval_ms": 0,
}


def settings_for(database, ledger):
    return Settings(
        database=database,
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


@pytest.mark.parametrize(
    "revision,code",
    [
        ("0040_owner_authority", "OWNER_AUTHORITY_DOWNGRADE"),
        ("0043_reader_authority", "OWNER_AUTHORITY_DOWNGRADE"),
        ("0044_voice_owner_authority", "VOICE_AUTHORITY_DOWNGRADE"),
        ("0045_goal_read_plan", "GOAL_PLAN_DOWNGRADE"),
        ("0061_singing_voice", "SINGING_DOWNGRADE"),
    ],
)
def test_authority_downgrade_keeps_custody_and_accounting_unchanged(core, tmp_path, revision, code):
    tables = ("runtime_meta", "voice_jobs", "voice_node_state", "model_usage", "outbox")
    path = historical_database(core.store, tmp_path / "authority-custody.db", revision)
    with closing(open_historical(path)) as original:
        before = table_snapshot(original, tables)
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    config.set_main_option("sqlalchemy.url", "sqlite:///" + path.as_posix())
    with pytest.raises(RuntimeError, match=code + "_REQUIRES_EXPLICIT_CUSTODY_RECONCILIATION"):
        command.downgrade(config, "0033_model_usage_job_index")
    with closing(open_historical(path)) as original:
        assert table_snapshot(original, tables) == before
        assert one(original, "SELECT version_num FROM alembic_version")["version_num"] == revision


@pytest.mark.parametrize("revision", ["0003_memory_index", "0004_diary"])
def test_populated_historical_upgrade_preserves_every_old_column_and_repeats(tmp_path, revision):
    database, _, before = seed_legacy(tmp_path, revision)
    migrate_sqlite(str(database), "0014_reader_budget")
    with closing(open_historical(str(database))) as store:
        assert rows(store, "SELECT version_num FROM alembic_version") == [
            {"version_num": "0014_reader_budget"}
        ]
        assert_old_columns_unchanged(store, before)
        assert_indexes_and_integrity(store)
        assert one(store, "SELECT version_num FROM alembic_version")["version_num"] == (
            "0014_reader_budget"
        )
        first_upgrade = table_snapshot(store, before)
    migrate_sqlite(str(database), "head")
    with closing(open_historical(str(database))) as store:
        incarnation = one(store, "SELECT * FROM runtime_meta WHERE key='voice_incarnation'")
        assert incarnation and re.fullmatch(r"[a-f0-9]{32}", incarnation["value"])
        first_upgrade["runtime_meta"]["rows"].append(incarnation)
        first_upgrade["runtime_meta"]["rows"].extend(
            [
                {"key": "owner_authority_restore_generation", "value": "initial"},
                {"key": "owner_authority_mode", "value": "OFF"},
            ]
        )
        assert_old_columns_unchanged(store, first_upgrade)
        for row in rows(store, "SELECT * FROM outbox"):
            assert {key: row[key] for key in SEGMENT_DEFAULTS} == SEGMENT_DEFAULTS
        final_upgrade = table_snapshot(store, before)
        assert_indexes_and_integrity(store)
        assert (
            one(store, "SELECT version_num FROM alembic_version")["version_num"]
            == (REVISION_CHAIN[-1])
        )
    migrate_sqlite(str(database), "head")
    with closing(open_historical(str(database))) as store:
        assert table_snapshot(store, before) == final_upgrade


@contextmanager
def restore_imported_history(tmp_path, archive, key, ledger, migrated_database, erase):
    """Explicitly import a historical SQLite archive before testing native restoration."""
    source = tmp_path / "stopped-source.sqlite3"
    source.write_bytes(Fernet(key).decrypt(archive.read_bytes()))
    assert source.read_bytes().startswith(b"SQLite format 3")
    migrate_sqlite(source)
    native_archive = tmp_path / "native.enc"
    with isolated_database(template=migrated_database) as imported:
        import_sqlite_backup(source, imported)
        backup(imported, native_archive, key)
        with (
            Database(imported) as database,
            closing(Store(database, tmp_path / "imported-state")) as current,
        ):
            erase(current, imported)
        with isolated_database() as destination:
            state = tmp_path / "restored-state"
            restore(native_archive, destination, key, ledger, state_directory=state)
            with Database(destination) as database, closing(Store(database, state)) as restored:
                yield restored


def native_rows(store, statement, parameters=()):
    # record_sequence is physical native ordering, not a historical business column.
    return [
        {key: value for key, value in row.items() if key != "record_sequence"}
        for row in store.rows(statement, parameters)
    ]


def native_one(store, statement, parameters=()):
    found = native_rows(store, statement, parameters)
    return found[0] if found else None


@pytest.mark.parametrize("forget_one", [False, True])
def test_encrypted_0003_backup_restores_with_newest_ledger_and_preserves_independent_fact(
    tmp_path,
    forget_one,
    migrated_database,
):
    database, ledger, before = seed_legacy(tmp_path, "0003_memory_index")
    archive, key = tmp_path / "old-0003.enc", Fernet.generate_key()
    archive.write_bytes(Fernet(key).encrypt(database.read_bytes()))
    ciphertext = archive.read_bytes()
    assert not ciphertext.startswith(b"SQLite") and TOKENS["keep"].encode() not in ciphertext
    assert Fernet(key).decrypt(ciphertext).startswith(b"SQLite format 3")

    def erase(current, _):
        if forget_one:
            with current.transaction(control_write=True):
                ErasureLedger(ledger).append(current, "mem_erase", Clock()())

    with restore_imported_history(
        tmp_path,
        archive,
        key,
        ledger,
        migrated_database,
        erase,
    ) as restored:
        generation = native_one(
            restored, "SELECT * FROM runtime_meta WHERE key='memory_control_restore_generation'"
        )
        assert generation and re.fullmatch(r"restore_[a-f0-9]{32}", generation["value"])
        restore_block = native_one(
            restored, "SELECT * FROM runtime_meta WHERE key='cloud_budget_restore_required'"
        )
        assert restore_block == {"key": "cloud_budget_restore_required", "value": "1"}
        assert native_rows(restored, "SELECT version_num FROM alembic_version") == [
            {"version_num": TARGET_REVISION}
        ]
        voice_incarnation = native_one(
            restored, "SELECT * FROM runtime_meta WHERE key='voice_incarnation'"
        )
        assert voice_incarnation and re.fullmatch(r"[a-f0-9]{32}", voice_incarnation["value"])
        voice_restore = native_one(
            restored, "SELECT * FROM runtime_meta WHERE key='voice_restore_required'"
        )
        assert voice_restore == {"key": "voice_restore_required", "value": "1"}
        assert restored.schema_capabilities()
        assert_native_indexes_and_integrity(
            restored, ("keep",) if forget_one else ("erase", "keep")
        )
        for table in ("memories", "memory_evidence", "memory_revisions", "memory_vectors"):
            expected = [row for row in before[table]["rows"] if row["memory_id"] == "mem_keep"]
            assert (
                native_rows(restored, f"SELECT * FROM {table} WHERE memory_id='mem_keep'")
                == expected
            )
        assert native_one(restored, "SELECT * FROM events WHERE event_id='evt_keep'") == {
            **next(row for row in before["events"]["rows"] if row["event_id"] == "evt_keep"),
            "reply_revision": 0,
        }
        for usage in before["model_usage"]["rows"]:
            expected = {
                **usage,
                "state": "UNKNOWN" if usage["state"] == "RESERVED" else usage["state"],
            }
            assert (
                native_one(
                    restored,
                    "SELECT * FROM model_usage WHERE request_id=%s",
                    (usage["request_id"],),
                )
                == expected
            )
        for old in before["outbox"]["rows"]:
            actual = native_one(
                restored, "SELECT * FROM outbox WHERE message_id=%s", (old["message_id"],)
            )
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
                    **old,
                    **SEGMENT_DEFAULTS,
                    "delivery_lane": "owner_private",
                    "native_ack_json": None,
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
            assert native_one(
                restored, "SELECT content,status FROM memories WHERE memory_id='mem_erase'"
            ) == {"content": "", "status": "FORGOTTEN"}
            assert native_one(
                restored, "SELECT body_json,redacted FROM events WHERE event_id='evt_erase'"
            ) == {"body_json": "{}", "redacted": 1}
            for table in ("memory_evidence", "memory_revisions", "memory_vectors"):
                assert (
                    native_rows(restored, f"SELECT * FROM {table} WHERE memory_id='mem_erase'")
                    == []
                )
            assert native_one(
                restored, "SELECT restore_min_replay_version,state FROM privacy_erasure_ledger"
            ) == {"restore_min_replay_version": 1, "state": "COMPLETE"}
        else:
            replay_contract = native_one(
                restored, "SELECT * FROM runtime_meta WHERE key=%s", (CONTRACT_KEY,)
            )
            assert replay_contract == {"key": CONTRACT_KEY, "value": contract_signature()}
            unchanged = {
                table: rows
                for table, rows in before.items()
                if table not in {"outbox", "model_usage"}
            }
            unchanged["runtime_meta"] = {
                **before["runtime_meta"],
                "rows": before["runtime_meta"]["rows"]
                + [voice_incarnation]
                + [
                    native_one(restored, "SELECT * FROM runtime_meta WHERE key=%s", (name,))
                    for name in ("owner_authority_restore_generation", "owner_authority_mode")
                ]
                + [generation, restore_block, replay_contract]
                + [
                    native_one(restored, "SELECT * FROM runtime_meta WHERE key=%s", (name,))
                    for name in (
                        "social_autonomy_restore_required",
                        "voice_restore_required",
                        "bili_video_restore_required",
                    )
                ],
            }
            assert_native_old_columns_unchanged(restored, unchanged)


def test_encrypted_0004_diary_restore_deletes_only_requested_date(tmp_path, migrated_database):
    database, ledger, before = seed_legacy(tmp_path, "0004_diary")
    archive, key = tmp_path / "old-0004.enc", Fernet.generate_key()
    archive.write_bytes(Fernet(key).encrypt(database.read_bytes()))

    def erase(_, imported):
        settings = settings_for(imported, ledger).model_copy(
            update={
                "state_directory": str(tmp_path / "diary-source-state"),
            }
        )
        with closing(Core(settings, clock=Clock())) as instance:
            instance.diary.forget(date.fromisoformat(TIMES["erase"][:10]), "10001")

    with restore_imported_history(
        tmp_path,
        archive,
        key,
        ledger,
        migrated_database,
        erase,
    ) as restored:
        assert native_one(
            restored, "SELECT status,body_md FROM diary_entries WHERE entry_id='diary_erase'"
        ) == {"status": "DELETED", "body_md": ""}
        for table in ("diary_evidence", "diary_revisions"):
            assert (
                native_rows(restored, f"SELECT * FROM {table} WHERE entry_id='diary_erase'") == []
            )
        for table in DIARY_TABLES:
            key_column = "run_id" if table == "diary_runs" else "entry_id"
            value = "diary_run_keep" if table == "diary_runs" else "diary_keep"
            assert native_rows(
                restored, f"SELECT * FROM {table} WHERE {key_column}=%s", (value,)
            ) == [row for row in before[table]["rows"] if row[key_column] == value]
        assert_native_old_columns_unchanged(
            restored,
            {
                table: before[table]
                for table in ("memories", "memory_evidence", "memory_revisions", "memory_vectors")
            },
        )
        assert_native_indexes_and_integrity(restored)


def test_0018_preserves_populated_goals_without_guessing_reflection_origins(tmp_path):
    database, _, _ = seed_legacy(tmp_path, "0004_diary")
    migrate_sqlite(str(database), "0017_paid_model_gate")
    with closing(open_historical(str(database))) as store:
        insert(
            store,
            "reflection_runs",
            run_id="reflection_legacy",
            identity_id="companion",
            local_date="2026-10-18",
            timezone="Asia/Shanghai",
            state="COMMITTED",
            source_set_sha256="a" * 64,
            captured_privacy_epoch=3,
            created_at=TIMES["keep"],
            updated_at=TIMES["keep"],
        )
        insert(
            store,
            "reflection_suggestions",
            suggestion_id="suggestion_legacy",
            run_id="reflection_legacy",
            kind="next_question",
            content="同样的历史主题",
            evidence_json="[]",
            state="PENDING",
            created_at=TIMES["keep"],
            updated_at=TIMES["keep"],
        )
        insert(
            store,
            "goals",
            goal_id="goal_legacy",
            kind="self",
            topic="同样的历史主题",
            scope_json=encode({"operation": "investigate", "url": ""}),
            proposal_key="legacy-key",
            state="CANDIDATE",
            priority=1,
            next_due_at=TIMES["keep"],
            created_at=TIMES["keep"],
            updated_at=TIMES["keep"],
        )
        insert(store, "goal_evidence", goal_id="goal_legacy", event_id="evt_keep")
        before = table_snapshot(
            store,
            (
                "goals",
                "goal_evidence",
                "reflection_runs",
                "reflection_suggestions",
                "events",
                "memories",
                "model_usage",
                "outbox",
                "runtime_meta",
            ),
        )
    # Exercise this historical migration's own downgrade guard, before later
    # irreversible custody migrations introduce their independent stop boundary.
    migrate_sqlite(str(database), "0018_reflection_goal_lineage")
    migrate_sqlite(str(database), "0018_reflection_goal_lineage")
    with closing(open_historical(str(database))) as store:
        assert_old_columns_unchanged(store, before)
        assert one(store, "SELECT reflection_suggestion_id FROM goals") == {
            "reflection_suggestion_id": None
        }
        assert rows(store, "PRAGMA foreign_key_check") == []
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
    with closing(open_historical(str(database))) as store:
        assert one(store, "SELECT reflection_suggestion_id FROM goals") == {
            "reflection_suggestion_id": "suggestion_legacy"
        }
        assert_old_columns_unchanged(store, before)
        assert one(store, "PRAGMA integrity_check")["integrity_check"] == "ok"


def test_0021_retains_reflection_provenance_and_guards_pending_wait_downgrade(tmp_path):
    database, _, _ = seed_legacy(tmp_path, "0004_diary")
    migrate_sqlite(str(database), "0020_reply_segment_capacity")
    tables = ("reflection_runs", "reflection_evidence", "reflection_suggestions", "model_usage")
    with closing(open_historical(str(database))) as store:
        for index, state in enumerate(("FAILED", "COMMITTED", "SKIPPED", "CANCELLED")):
            run_id = "old_reflection_" + str(index)
            insert(
                store,
                "reflection_runs",
                run_id=run_id,
                identity_id="companion",
                local_date=f"2026-10-{10 + index}",
                timezone="Asia/Shanghai",
                state=state,
                source_set_sha256="a" * 64,
                captured_privacy_epoch=2,
                last_error_code="SCENARIO_CALL_LIMIT" if state == "FAILED" else None,
                created_at=TIMES["keep"],
                updated_at=TIMES["keep"],
            )
            insert(
                store,
                "reflection_evidence",
                run_id=run_id,
                event_id="evt_keep",
                source_id="owner:evt_keep",
            )
        insert(
            store,
            "reflection_suggestions",
            suggestion_id="old_suggestion",
            run_id="old_reflection_1",
            kind="next_question",
            content="继续了解银杉航线",
            evidence_json="[]",
            state="PENDING",
            created_at=TIMES["keep"],
            updated_at=TIMES["keep"],
        )
        before = table_snapshot(store, tables)
    migrate_sqlite(str(database), "0021_reflection_admission_retry")
    migrate_sqlite(str(database), "0021_reflection_admission_retry")
    with closing(open_historical(str(database))) as store:
        assert_old_columns_unchanged(store, before)
        assert all(
            row["next_attempt_at"] is None for row in rows(store, "SELECT * FROM reflection_runs")
        )
        assert rows(store, "PRAGMA foreign_key_check") == []
        store.execute(
            "UPDATE reflection_runs SET state='RETRY_WAIT',next_attempt_at=? "
            "WHERE run_id='old_reflection_0'",
            (TIMES["keep"],),
        )
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
    with closing(open_historical(str(database))) as store:
        assert table_snapshot(store, tables) == pending
        store.execute(
            "UPDATE reflection_runs SET state='FAILED',next_attempt_at=NULL "
            "WHERE run_id='old_reflection_0'"
        )
    command.downgrade(config, "0020_reply_segment_capacity")
    with closing(open_historical(str(database))) as store:
        assert table_snapshot(store, tables) == before
        assert rows(store, "PRAGMA foreign_key_check") == []


def test_0033_job_index_preserves_rows_rowids_budget_snapshots_and_erasure_ledger(tmp_path):
    database, ledger, _ = seed_legacy(tmp_path, "0004_diary")
    migrate_sqlite(str(database), "0032_bili_video")
    with closing(open_historical(str(database))) as store:
        original = one(store, "SELECT * FROM model_usage WHERE state='UNKNOWN'")
        # A gap makes accidental table rebuilds visible, including the NULL-job case.
        insert(
            store,
            "model_usage",
            rowid=41,
            **{
                **original,
                "usage_id": "usage_null_job",
                "request_id": "request_null_job",
                "job_id": None,
            },
        )
        insert(
            store, "model_budget_windows", window_id="migration-window", contract_sha256="a" * 64
        )
        for index, row in enumerate(rows(store, "SELECT * FROM model_usage ORDER BY rowid")):
            insert(
                store,
                "model_admissions",
                request_id=row["request_id"],
                logical_request_id="job_keep",
                attempt_id=f"attempt_{index}",
                attempt_number=index + 1,
                window_id="migration-window",
                contract_sha256="a" * 64,
                payload_sha256="b" * 64,
                snapshot_json=encode({"historical_pricing": True}),
                reserved_micro_usd=row["billed_micro_usd"],
                input_bound=1000,
                output_bound=100,
                lease_token=f"lease_{index}",
                lease_until=TIMES["keep"],
                io_started_at=TIMES["keep"],
                journal_path=str(tmp_path / "retained-budget.jsonl"),
                journal_id="retained-budget",
            )
        config = cloud_budget.CloudBudgetConfig().model_dump()
        insert(
            store,
            "cloud_budget_policies",
            version=1,
            config_json=encode(config),
            config_sha256="c" * 64,
            timezone="Asia/Shanghai",
            effective_at=TIMES["keep"],
            fx_valid_from=TIMES["erase"],
            fx_valid_until=TIMES["keep"],
        )
        for usage in rows(store, "SELECT * FROM model_usage ORDER BY rowid"):
            insert(
                store,
                "cloud_cost_snapshots",
                reference_type="model",
                reference_id=usage["request_id"],
                policy_version=1,
                task_kind="model",
                task_id=usage["job_id"] or usage["request_id"],
                local_day=TIMES["keep"][:10],
                occurred_at=usage["occurred_at"],
                snapshot_json=encode({"config": config}),
                reserved_micro_usd=usage["billed_micro_usd"],
                accounted_micro_usd=usage["billed_micro_usd"],
                amount_micro_cny=usage["billed_micro_usd"] * 7,
                state=usage["state"],
                legacy=1,
                updated_at=TIMES["keep"],
            )
        assert one(store, "SELECT COUNT(*) n FROM cloud_cost_snapshots")["n"] == 4
        tables = rows(
            store,
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name<>'alembic_version' "
            "ORDER BY name",
        )
        schema = rows(
            store,
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name<>'alembic_version' ORDER BY name",
        )

        def rows_with_rowids(current):
            snapshot = {}
            for table in tables:
                name = table["name"]
                if "WITHOUT ROWID" in " ".join(table["sql"].upper().split()):
                    primary = sorted(
                        (row for row in rows(current, f'PRAGMA table_info("{name}")') if row["pk"]),
                        key=lambda row: row["pk"],
                    )
                    assert primary
                    order = ",".join('"' + row["name"] + '"' for row in primary)
                    query = f'SELECT * FROM "{name}" ORDER BY {order}'
                else:
                    query = f'SELECT rowid AS snapshot_rowid,* FROM "{name}" ORDER BY rowid'
                snapshot[name] = rows(current, query)
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
            migrate_sqlite(str(database), "0033_model_usage_job_index")
        else:
            command.downgrade(config, "0032_bili_video")
        with closing(open_historical(str(database))) as store:
            assert rows_with_rowids(store) == before
            assert ledger.read_bytes() == ledger_bytes
            assert (
                rows(
                    store,
                    "SELECT type,name,tbl_name,sql FROM sqlite_master "
                    "WHERE name NOT IN ('alembic_version','idx_model_usage_job') "
                    "ORDER BY name",
                )
                == schema
            )
            assert rows(store, "PRAGMA foreign_key_check") == []
            assert one(store, "PRAGMA integrity_check") == {"integrity_check": "ok"}
            assert one(store, "SELECT version_num FROM alembic_version")["version_num"] == (
                "0033_model_usage_job_index" if operation == "upgrade" else "0032_bili_video"
            )
            indexes = rows(store, "PRAGMA index_list(model_usage)")
            actual = [row for row in indexes if row["name"] == "idx_model_usage_job"]
            if operation == "upgrade":
                assert len(actual) == 1 and actual[0]["unique"] == 0
                assert [
                    row["name"] for row in rows(store, "PRAGMA index_info(idx_model_usage_job)")
                ] == ["job_id"]
                plan = rows(
                    store,
                    "EXPLAIN QUERY PLAN SELECT request_id FROM model_usage WHERE job_id=?",
                    ("job_keep",),
                )
                assert any("idx_model_usage_job" in row["detail"] for row in plan)
            else:
                assert actual == []
