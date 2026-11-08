"""Restore yields within its SQL deadline without replaying paid or tool effects."""

import time
from contextlib import nullcontext
from types import SimpleNamespace

import psycopg
import pytest

from companion import agent_chat, retrieval_work
from companion.contracts import CoreError
from tests.conftest import run


def test_native_deadline_cancels_sql_and_restores_connection(core):
    with pytest.raises(psycopg.errors.QueryCanceled), core.store.deadline(.05):
        with core.store.read_snapshot():
            core.store.one("SELECT pg_sleep(2)")
    assert not core.store.in_transaction
    assert core.store.one("SHOW statement_timeout") == {"statement_timeout": "0"}
    assert core.store.one("SELECT 1 AS value") == {"value": 1}


def test_deadline_is_total_across_queries_and_nested_scope_cannot_extend_it(core):
    with core.store.deadline(.12):
        core.store.one("SELECT pg_sleep(.08)")
        with core.store.deadline(2):
            with pytest.raises(psycopg.errors.QueryCanceled):
                core.store.one("SELECT pg_sleep(.08)")
    assert core.store.one("SHOW statement_timeout") == {"statement_timeout": "0"}


def test_existing_server_timeout_is_preserved(core):
    core.store.execute("SET statement_timeout='10ms'")
    try:
        with pytest.raises(psycopg.errors.QueryCanceled), core.store.deadline(2):
            core.store.one("SELECT pg_sleep(1)")
        assert core.store.one("SHOW statement_timeout") == {"statement_timeout": "10ms"}
    finally:
        core.store.execute("SET statement_timeout=0")


@pytest.mark.parametrize("sql", [False, True])
def test_permanent_restore_contention_stops_before_any_effect(core, monkeypatch, sql):
    core.settings.chat_restore_timeout_seconds = .08
    attempts = []
    monkeypatch.setattr(agent_chat, "check_conversation", lambda *_a: None)

    class Saved:
        def __init__(self, *_a):
            pass

        def load(self):
            attempts.append(1)
            if sql:
                core.store.one("SELECT pg_sleep(2)")
            raise psycopg.errors.SerializationFailure("synthetic restore conflict")

    monkeypatch.setattr(agent_chat, "ConversationStore", Saved)
    started = time.monotonic()
    with pytest.raises(CoreError, match="CONVERSATION_RESTORE_BUSY"):
        run(agent_chat.reply(core, {"trace_id": "trace_restore"}, []))
    assert time.monotonic() - started < 1
    assert attempts and not core.model.calls
    assert not core.store.rows("SELECT * FROM model_usage")
    assert not core.store.rows("SELECT * FROM outbox")


def test_transient_restore_conflict_reuses_completed_output(core, monkeypatch):
    attempts = []
    monkeypatch.setattr(agent_chat, "check_conversation", lambda *_a: SimpleNamespace())

    class Saved:
        def __init__(self, *_a):
            pass

        def load(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise psycopg.errors.DeadlockDetected("synthetic first attempt")
            return None

        def row(self):
            return {"phase": "OUTPUT", "output_text": "原来的已完成答复"}

    monkeypatch.setattr(agent_chat, "ConversationStore", Saved)
    text, _ = run(agent_chat.reply(core, {"trace_id": "trace_restore"}, []))
    assert text == "原来的已完成答复" and len(attempts) == 2
    assert not core.model.calls


def test_required_source_timeout_yields_instead_of_becoming_a_miss(core, monkeypatch):
    core.settings.chat_restore_timeout_seconds = .04

    def unavailable(*_args):
        raise retrieval_work.WorkLimit("sql_milliseconds")

    monkeypatch.setattr(agent_chat, "check_conversation", unavailable)
    with pytest.raises(CoreError, match="CONVERSATION_RESTORE_BUSY"):
        run(agent_chat.reply(core, {"trace_id": "trace_restore"}, []))
    assert not core.model.calls


@pytest.mark.parametrize("restore_deadline", [False, True])
def test_required_source_sql_timeout_rolls_back_only_its_savepoint(
        core, monkeypatch, restore_deadline):
    monkeypatch.setitem(retrieval_work.LIMITS, "sql_milliseconds", 5)
    deadline = core.store.deadline(5) if restore_deadline else nullcontext()
    with deadline, core.store.transaction(control_write=True), retrieval_work.scope() as work:
        core.store.execute("INSERT INTO runtime_meta(key,value) VALUES ('deadline-proof','kept')")
        started = time.monotonic()
        with pytest.raises(retrieval_work.WorkLimit):
            work.source_rows(core.store, "SELECT pg_sleep(2)", limit=1)
        assert time.monotonic() - started < .5
        assert core.store.one("SELECT 1 AS value") == {"value": 1}
    assert core.store.one("SHOW statement_timeout") == {"statement_timeout": "0"}
    assert core.store.one("SELECT value FROM runtime_meta WHERE key='deadline-proof'") == {
        "value": "kept"}
