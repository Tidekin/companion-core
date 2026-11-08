"""Split replies still use the original durable delivery and privacy boundaries."""

import json
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

from companion.backup import backup, restore
from companion.contracts import CoreError, DeliveryResult, LeaseRequest
from companion.legacy_import import migrate_sqlite
from companion.postgres import Database
from companion.reply_segments import SegmentedReplyConfig
from companion.store import Store
from companion.transport import (
    begin_send,
    check_segment_ready,
    delivery_result,
    enqueue_reply_parts,
    lease_deliveries,
    reconcile_delivery,
    recover_deliveries,
    reply_group,
)
from tests.conftest import ingest, run
from tests.postgres_fixture import logical_state
from tests.support.historical_schema import historical_database
from tests.support.release_migration import one, open_historical, rows
from tests.support.segmented_delivery import PARTS, acknowledge, freeze, lease


def test_group_is_atomic_idempotent_and_content_is_not_duplicated(core, monkeypatch):
    ingest(core)
    job = core.store.one("SELECT j.*,e.conversation_id FROM jobs j JOIN events e USING(event_id)")
    insert = core.store.insert
    count = 0

    def fail_second(table, **values):
        nonlocal count
        if table == "outbox":
            count += 1
            if count == 2:
                raise RuntimeError("synthetic insertion failure")
        return insert(table, **values)

    with monkeypatch.context() as patch:
        patch.setattr(core.store, "insert", fail_second)
        with pytest.raises(RuntimeError, match="synthetic"):
            enqueue_reply_parts(core.store, job, PARTS, 1500, core.clock())
    assert core.store.rows("SELECT * FROM outbox") == []
    ids = enqueue_reply_parts(core.store, job, PARTS, 1500, core.clock())
    assert enqueue_reply_parts(core.store, job, PARTS, 1500, core.clock()) == ids
    with pytest.raises(CoreError, match="REPLY_GROUP_CHANGED"):
        enqueue_reply_parts(core.store, job, PARTS[:-1], 1500, core.clock())
    group = reply_group(core.store, ids[1])
    assert group["text"] == "".join(PARTS)
    assert [json.loads(row["content_json"])["text"] for row in group["rows"]] == PARTS
    assert all(set(json.loads(row["content_json"])) == {"text"} for row in group["rows"])
    assert core.store.rows(
        "SELECT conname FROM pg_constraint WHERE connamespace=current_schema()::regnamespace "
        "AND contype='f' AND NOT convalidated") == []


def historical_parts(store, ids):
    parts = rows(store, "SELECT message_id,content_json FROM outbox "
                        "WHERE reply_group_id=(SELECT reply_group_id FROM outbox "
                        "WHERE message_id=?) ORDER BY segment_index", (ids[0],))
    assert [row["message_id"] for row in parts] == ids
    return [json.loads(row["content_json"])["text"] for row in parts]


def test_capacity_upgrade_preserves_existing_groups_indexes_and_foreign_keys(core, tmp_path):
    def index_definitions(store):
        # Batch recreation can reformat DDL without changing index semantics.
        return {row["name"]: "".join(row["sql"].split()) if row["sql"] else None
                for row in rows(store, "SELECT name,sql FROM sqlite_master WHERE type='index' "
                                      "AND tbl_name='outbox'")}

    _, ids = freeze(core)
    database = historical_database(core.store, tmp_path / "capacity.db", "0019_segmented_replies")
    with closing(open_historical(database)) as store:
        before = rows(store, "SELECT * FROM outbox ORDER BY rowid")
        indexes = index_definitions(store)
    migrate_sqlite(database, "0020_reply_segment_capacity")
    migration = Config()
    migration.set_main_option("script_location", str(Path("migrations").resolve()))
    migration.set_main_option("sqlalchemy.url", "sqlite:///" + database.as_posix())
    with closing(open_historical(database)) as store:
        assert rows(store, "SELECT * FROM outbox ORDER BY rowid") == before
        assert index_definitions(store) == indexes
        assert rows(store, "PRAGMA foreign_key_check") == []
        assert historical_parts(store, ids) == PARTS
    command.downgrade(migration, "0019_segmented_replies")
    with closing(open_historical(database)) as store:
        assert rows(store, "SELECT * FROM outbox ORDER BY rowid") == before
    ingest(core, 2)
    job = core.store.one("SELECT j.*,e.conversation_id FROM jobs j JOIN events e USING(event_id) "
                         "WHERE j.event_id='evt_test_2'")
    expanded = enqueue_reply_parts(core.store, job, PARTS + PARTS[:2], 0, core.clock())
    assert len(expanded) == 5
    expanded_database = historical_database(
        core.store, tmp_path / "expanded.db", "0020_reply_segment_capacity")
    migration.set_main_option("sqlalchemy.url", "sqlite:///" + expanded_database.as_posix())
    with pytest.raises(
        RuntimeError, match="REPLY_CAPACITY_DOWNGRADE_REQUIRES_EXPLICIT_DATA_REVIEW",
    ):
        command.downgrade(migration, "0019_segmented_replies")
    with closing(open_historical(expanded_database)) as store:
        assert historical_parts(store, expanded) == PARTS + PARTS[:2]
        assert rows(store, "PRAGMA foreign_key_check") == []


def test_two_workers_cannot_lease_later_parts_and_ack_starts_the_gap(core):
    _, ids = freeze(core)
    first = lease(core)[0]
    assert first["message_id"] == ids[0]
    assert first["reply_segment"]["count"] == 3
    assert lease(core, "second-worker") == []
    acknowledge(core, first)
    assert lease(core) == []
    core.clock.advance(1.49)
    assert lease(core) == []
    core.clock.advance(0.01)
    second = lease(core)[0]
    assert second["message_id"] == ids[1]
    acknowledge(core, second)
    core.clock.advance(1.5)
    third = lease(core)[0]
    acknowledge(core, third)
    assert lease(core) == []
    assert reply_group(core.store, ids[0], require_sent=True)["text"] == "".join(PARTS)
    assert [r["attempt_count"] for r in core.store.rows(
        'SELECT * FROM outbox ORDER BY record_sequence'
    )] == [
        1,
        1,
        1,
    ]


@pytest.mark.parametrize("failure", ["DELIVERY_UNCERTAIN", "PERMANENT_FAILURE", "expired"])
def test_partial_failure_cancels_remaining_parts_and_late_ack_cannot_resume(core, failure):
    _, ids = freeze(core)
    first = lease(core)[0]
    acknowledge(core, first)
    core.clock.advance(1.5)
    second = lease(core)[0]
    if failure == "expired":
        begin_send(
            core.store, second["message_id"], second["lease_token"], core.clock(), core.settings
        )
        core.clock.advance(core.settings.lease_seconds + 1)
        with core.store.transaction():
            recover_deliveries(core.store, core.clock())
    else:
        acknowledge(core, second, failure)
    states = [r["state"] for r in core.store.rows('SELECT * FROM outbox ORDER BY record_sequence')]
    assert states == [
        "SENT",
        "FAILED" if failure == "PERMANENT_FAILURE" else "DELIVERY_UNCERTAIN",
        "CANCELLED",
    ]
    assert lease(core) == []
    assert reply_group(core.store, ids[0], require_sent=True) is None
    if failure != "PERMANENT_FAILURE":
        reconcile_delivery(core.store, ids[1], "SENT", core.clock())
        assert lease(core) == []
        assert (
            core.store.one('SELECT state FROM outbox WHERE message_id=%s', (ids[2],))["state"]
            == "CANCELLED"
        )


def test_uncertain_result_cancels_its_own_suffix_and_recovery_repairs_other_groups(core):
    job, ids = freeze(core)
    item, = lease(core)
    begin_send(core.store, ids[0], item["lease_token"], core.clock(), core.settings)
    other = ingest(core, 2, origin=f"other:FriendMessage:{core.settings.owner_qq_id}")
    other_job = core.store.one(
        'SELECT j.*,e.conversation_id FROM jobs j JOIN events e USING(event_id) WHERE job_id=%s',
        (other["job_id"],),
    )
    other_ids = enqueue_reply_parts(core.store, other_job, PARTS, 1500, core.clock())
    # An older group already needs recovery. Reporting this message must only change its group.
    core.store.execute("UPDATE outbox SET state='FAILED' WHERE message_id=%s", (other_ids[0],))
    delivery_result(core.store, core.settings, ids[0], DeliveryResult(
        lease_token=item["lease_token"], result="DELIVERY_UNCERTAIN",
    ), core.clock())

    def rows(job_id):
        return core.store.rows((
            'SELECT state,last_error_code,updated_at FROM outbox WHERE '
            'reply_group_id=%s ORDER BY segment_index'
        ), (job_id,))

    original = rows(job["job_id"])
    assert [row["state"] for row in original] == ["DELIVERY_UNCERTAIN", "CANCELLED", "CANCELLED"]
    assert [row["state"] for row in rows(other_job["job_id"])] == ["FAILED", "PENDING", "PENDING"]
    core.clock.advance(1)
    with core.store.transaction():
        recover_deliveries(core.store, core.clock())
    assert rows(job["job_id"]) == original
    assert [row["state"] for row in rows(other_job["job_id"])] == [
        "FAILED", "CANCELLED", "CANCELLED"]


def test_new_accepted_owner_input_cancels_the_unsent_tail_but_duplicate_does_not(core):
    _, ids = freeze(core)
    first = lease(core)[0]
    acknowledge(core, first)
    ingest(core, text="一起聊一聊接下来的计划")
    assert (
        core.store.one('SELECT state FROM outbox WHERE message_id=%s', (ids[1],))["state"]
        == "PENDING"
    )
    ingest(core, 2, "先等一下，我想换个话题")
    core.clock.advance(10)
    assert lease(core) == []
    assert [r["state"] for r in core.store.rows(
        'SELECT * FROM outbox ORDER BY record_sequence'
    )] == [
        "SENT",
        "CANCELLED",
        "CANCELLED",
    ]


def test_late_ack_before_recovery_records_delivery_without_resuming_expired_chain(core):
    _, ids = freeze(core)
    first = lease(core)[0]
    begin_send(core.store, first["message_id"], first["lease_token"], core.clock(), core.settings)
    core.clock.advance(core.settings.lease_seconds + 1)
    delivery_result(core.store, core.settings, first["message_id"], DeliveryResult(
        lease_token=first["lease_token"], result="SENT", provider_message_id="111",
    ), core.clock())
    core.clock.advance(2)
    assert lease(core) == []
    assert [row["state"] for row in core.store.rows(
        'SELECT * FROM outbox ORDER BY record_sequence'
    )] == [
        "SENT", "CANCELLED", "CANCELLED",
    ]
    assert reply_group(core.store, ids[0], require_sent=True) is None


@pytest.mark.parametrize("mutation", ["text", "count", "manifest", "interval", "missing"])
def test_corrupt_group_cannot_be_leased_or_sent(core, mutation):
    _, ids = freeze(core)
    if mutation == "missing":
        core.store.execute('DELETE FROM outbox WHERE message_id=%s', (ids[-1],))
    else:
        field, value = {
            "text": ("content_json", json.dumps({"text": "changed"})),
            "count": ("segment_count", 2),
            "manifest": ("reply_group_sha256", "0" * 64),
            "interval": ("segment_interval_ms", 0),
        }[mutation]
        core.store.execute(f"UPDATE outbox SET {field}=%s WHERE message_id=%s", (value, ids[1]))
    with pytest.raises(CoreError, match="REPLY_GROUP"):
        check_segment_ready(core.store, ids[0], core.clock())
    with pytest.raises(CoreError, match="REPLY_GROUP"):
        lease(core)
    assert core.store.one("SELECT SUM(attempt_count) AS n FROM outbox")["n"] == 0


def test_core_splits_one_model_reply_but_preserves_default_single_reply(core):
    core.settings.segmented_reply = SegmentedReplyConfig(min_chars=4)
    ingest(core, text="".join(PARTS))
    assert run(core.run_once())
    rows = core.store.rows('SELECT * FROM outbox ORDER BY record_sequence')
    assert len(rows) == 3 and len(core.model.calls) == 1
    assert "".join(json.loads(row["content_json"])["text"] for row in rows) == "回复：" + "".join(
        PARTS
    )
    core.settings.segmented_reply = None
    ingest(core, 2, "".join(PARTS))
    assert run(core.run_once())
    rows = core.store.rows("SELECT * FROM outbox WHERE origin_event_id='evt_test_2'")
    assert len(rows) == 1 and rows[0]["delivery_key"] == "reply:evt_test_2:v1"


def test_new_input_accepted_during_generation_prevents_stale_segment_group(core):
    core.settings.segmented_reply = SegmentedReplyConfig(min_chars=4)
    ingest(core, text="".join(PARTS))
    core.model.callback = lambda: ingest(core, 2, "先等一下，我想换个话题")
    assert run(core.run_once())
    assert len(core.model.calls) == 1
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.store.one("SELECT state FROM jobs WHERE event_id='evt_test_1'") == {
        "state": "SUCCEEDED",
    }
    assert core.store.one("SELECT state FROM jobs WHERE event_id='evt_test_2'") == {
        "state": "QUEUED",
    }


def test_all_parts_are_erased_and_old_encrypted_backup_cannot_restore_them(
    core, tmp_path, empty_database
):
    job, ids = freeze(core)
    acknowledge(core, lease(core)[0])
    core.clock.advance(1.5)
    second = lease(core)[0]
    begin_send(core.store, ids[1], second["lease_token"], core.clock(), core.settings)
    archive, restored_path = tmp_path / "before-erasure.enc", empty_database
    key = Fernet.generate_key()
    backup(core.settings.database, archive, key)
    assert PARTS[0].encode() not in archive.read_bytes()
    with core.store.transaction(control_write=True):
        core.ledger.expire_events(core.store, [job["event_id"]], core.clock())
    restore(archive, restored_path, key, core.settings.erasure_ledger,
            state_directory=tmp_path / "restored-state")
    with (
        Database(restored_path) as database,
        closing(Store(database, tmp_path / "restored-state")) as restored,
    ):
        for store in (core.store, restored):
            rows = store.rows("SELECT * FROM outbox ORDER BY segment_index")
            assert [row["message_id"] for row in rows] == ids
            assert [row["state"] for row in rows] == ["SENT", "DELIVERY_UNCERTAIN", "CANCELLED"]
            assert all(row["content_json"] == "{}" and row["content_sha256"] == "" for row in rows)
            assert store.one("SELECT body_json,redacted FROM events WHERE event_id=%s", (
                job["event_id"],
            )) == {"body_json": "{}", "redacted": 1}
            with pytest.raises(CoreError, match="REPLY_GROUP_CHANGED"):
                reply_group(store, ids[0])
            assert lease_deliveries(store, core.settings, LeaseRequest(
                worker_id="restored-test", limit=5,
            ), core.clock())["items"] == []
            assert not store.rows(
                "SELECT conname FROM pg_constraint "
                "WHERE connamespace=current_schema()::regnamespace "
                "AND contype='f' AND NOT convalidated"
            )
        assert all(
            part not in json.dumps(logical_state(restored), ensure_ascii=False) for part in PARTS
        )


def test_downgrade_refuses_to_discard_existing_segment_group(core, tmp_path):
    _, ids = freeze(core)
    database = historical_database(core.store, tmp_path / "segments.db", "0019_segmented_replies")
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    config.set_main_option("sqlalchemy.url", "sqlite:///" + database.as_posix())
    with closing(open_historical(database)) as store:
        before = rows(store, "SELECT * FROM outbox ORDER BY rowid")
    with pytest.raises(RuntimeError, match="SEGMENTED_REPLIES_DOWNGRADE_REQUIRES_EXPLICIT_REVIEW"):
        command.downgrade(config, "0018_reflection_goal_lineage")
    with closing(open_historical(database)) as store:
        assert rows(store, "SELECT * FROM outbox ORDER BY rowid") == before
        assert one(store, "SELECT version_num FROM alembic_version")["version_num"] == (
            "0019_segmented_replies")
        assert historical_parts(store, ids) == PARTS
        assert not rows(store, "PRAGMA foreign_key_check")
