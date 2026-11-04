"""Schema changes occur only at explicit preparation, never during validation."""

import sqlite3
from contextlib import asynccontextmanager

import pytest

from companion_onebot_bridge.outbound import validate_item
from companion_onebot_bridge.spool import Spool
from companion_onebot_bridge.window import SendOperation, TestWindow, digest
from tests.conftest import run
from tests.support.onebot_events import normalized, raw_event
from tests.test_onebot_window import input_approval
from tests.test_onebot_window_handoff import CoreContract, plan_for


@asynccontextmanager
async def opened_window(tmp_path):
    cfg, plan, now = plan_for(tmp_path)
    contract = CoreContract(plan)
    with Spool(cfg) as spool:
        async with contract.client() as client:
            window = TestWindow(spool, plan, clock=lambda: now)
            await window.open(client)
            yield window, client


def unique_attempts(window):
    """Prepare a complete persisted schema from before fragment support."""
    with window.db:
        window.db.execute("DROP TABLE window_attempts")
        window.db.execute(
            "CREATE TABLE window_attempts(message_id TEXT PRIMARY KEY,"
            "event_id TEXT NOT NULL UNIQUE,fence INTEGER NOT NULL,state TEXT NOT NULL,"
            "text_hash TEXT NOT NULL,approval_hash TEXT NOT NULL,provider_ack_hash TEXT,"
            "item_hash TEXT NOT NULL,plan_hash TEXT NOT NULL)"
        )
        window.db.execute(
            "INSERT INTO window_attempts(rowid,message_id,event_id,fence,state,text_hash,"
            "approval_hash,provider_ack_hash,item_hash,plan_hash) "
            "VALUES(9,'msg_unknown','evt_unknown',1,'UNKNOWN','text','approval',NULL,'item',?)",
            (window.plan.sha256,),
        )


@pytest.mark.parametrize("old_unique", [False, True])
def test_runtime_schema_checks_never_write_or_inspect_indexes(tmp_path, old_unique):
    async def scenario():
        async with opened_window(tmp_path) as (window, _):
            if old_unique:
                unique_attempts(window)
            before = list(window.db.iterdump())
            journal = window.plan.journal.read_bytes()
            queries = []
            window.db.set_trace_callback(queries.append)
            try:
                window.verify(active=False)
                status = window.status()
            finally:
                window.db.set_trace_callback(None)
            assert status["attempt_states"] == ({"UNKNOWN": 1} if old_unique else {})
            assert queries and all(query.lstrip().upper().startswith("SELECT") for query in queries)
            assert all("pragma" not in query.lower() for query in queries)
            assert list(window.db.iterdump()) == before
            assert window.plan.journal.read_bytes() == journal

    run(scenario())


@pytest.mark.parametrize("missing", ["input_owner", "attempt_owner", "history"])
def test_missing_schema_is_refused_without_assigning_ownership(tmp_path, missing):
    async def scenario():
        async with opened_window(tmp_path) as (window, _):
            statement = {
                "input_owner": "ALTER TABLE window_inputs DROP COLUMN plan_hash",
                "attempt_owner": "ALTER TABLE window_attempts DROP COLUMN plan_hash",
                "history": "DROP TABLE window_history",
            }[missing]
            with window.db:
                window.db.execute(statement)
            before = list(window.db.iterdump())
            changes = window.db.total_changes
            journal = window.plan.journal.read_bytes()
            checks = (lambda: window.verify(active=False), window.status, window.prepare_schema)
            for check in checks:
                with pytest.raises(ValueError, match="WINDOW_SCHEMA_PREPARATION_REQUIRED"):
                    check()
                assert list(window.db.iterdump()) == before
                assert window.db.total_changes == changes
                assert window.plan.journal.read_bytes() == journal

    run(scenario())


def test_explicit_preparation_preserves_all_rows_and_is_idempotent(tmp_path):
    async def scenario():
        async with opened_window(tmp_path) as (window, _):
            unique_attempts(window)
            with window.db:
                window.db.execute(
                    "INSERT INTO window_attempts(rowid,message_id,event_id,fence,state,text_hash,"
                    "approval_hash,provider_ack_hash,item_hash,plan_hash) "
                    "VALUES(42,'msg_ack','evt_ack',2,'ACK','text2','approval2','provider2',"
                    "'item2',?)", (window.plan.sha256,),
                )
            before = [dict(row) for row in window.db.execute(
                "SELECT rowid,* FROM window_attempts ORDER BY rowid",
            )]
            journal = window.plan.journal.read_bytes()
            window.prepare_schema()
            assert [dict(row) for row in window.db.execute(
                "SELECT rowid,* FROM window_attempts ORDER BY rowid",
            )] == before
            dump = list(window.db.iterdump())
            window.prepare_schema()
            assert list(window.db.iterdump()) == dump
            assert window.plan.journal.read_bytes() == journal
            with pytest.raises(ValueError, match="WINDOW_ATTEMPT_RECONCILIATION_REQUIRED"):
                window.verify()

    run(scenario())


@pytest.mark.parametrize("extra", ["column", "index", "trigger", "constraint"])
def test_preparation_does_not_discard_unrecognized_schema(tmp_path, extra):
    async def scenario():
        async with opened_window(tmp_path) as (window, _):
            unique_attempts(window)
            statement = {
                "column": "ALTER TABLE window_attempts ADD COLUMN owner_note TEXT",
                "index": "CREATE INDEX custom_window_fence ON window_attempts(fence)",
                "trigger": "CREATE TRIGGER custom_window_insert AFTER INSERT ON window_attempts "
                           "BEGIN SELECT 1; END",
                "constraint": "CREATE UNIQUE INDEX custom_window_fence ON window_attempts(fence)",
            }[extra]
            with window.db:
                window.db.execute(statement)
            before = list(window.db.iterdump())
            with pytest.raises(ValueError, match="WINDOW_SCHEMA_AMBIGUOUS"):
                window.prepare_schema()
            assert list(window.db.iterdump()) == before

    run(scenario())


def test_failed_schema_copy_rolls_back_before_losing_the_original(tmp_path):
    async def scenario():
        async with opened_window(tmp_path) as (window, _):
            unique_attempts(window)
            before = list(window.db.iterdump())

            def deny_drop(action, name, _column, _database, _source):
                return (sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DROP_TABLE
                        and name == "window_attempts" else sqlite3.SQLITE_OK)

            window.db.set_authorizer(deny_drop)
            try:
                with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                    window.prepare_schema()
            finally:
                window.db.set_authorizer(None)
            assert list(window.db.iterdump()) == before

    run(scenario())


def test_plain_reply_cannot_reserve_twice_without_event_unique_constraint(tmp_path):
    async def scenario():
        async with opened_window(tmp_path) as (window, client):
            indexes = window.db.execute("PRAGMA index_list(window_attempts)").fetchall()
            assert [row[3] for row in indexes] == ["pk"]
            raw = raw_event(1)
            event = normalized(raw, window.plan.config)
            await window.ingest(client, raw, input_approval(window.plan, raw))

            def operation(message_id):
                item = {"message_id": message_id, "delivery_key": f"reply:{event['event_id']}:v1",
                        "action_type": "qq.private.reply",
                        "conversation_platform_session_key": window.plan.config.origin,
                        "lease_token": "lease_" + message_id.removeprefix("msg_"),
                        "content": {"text": "one ordinary reply"},
                        "lease_until": window.plan.end.isoformat()}
                validate_item(item, window.plan.config)
                approval = {"window_sha256": window.plan.sha256, "event_id": event["event_id"],
                            "message_id": message_id,
                            "reply_sha256": digest(item["content"]["text"]),
                            "evidence_sha256": "a" * 64}
                return SendOperation(window, client, approval), item

            first, item = operation("msg_" + "1" * 32)
            first.reserve(item)
            first.complete(item, "synthetic-provider-ack")
            before = list(window.db.iterdump())
            journal = window.plan.journal.read_bytes()
            for message_id in (item["message_id"], "msg_" + "2" * 32):
                second, repeated = operation(message_id)
                with pytest.raises(ValueError, match="REPLY_SEGMENT_SEQUENCE_REQUIRED"):
                    second.reserve(repeated)
                assert list(window.db.iterdump()) == before
                assert window.plan.journal.read_bytes() == journal

    run(scenario())
