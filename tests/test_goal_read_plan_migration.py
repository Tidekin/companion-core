"""Additive native step custody migrates real prior schema without reinterpreting promises."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from companion.store import Store, encode, migrate
from tests.support.historical_schema import historical_database


@pytest.mark.parametrize("state", ["RUNNING", "EXECUTION_UNCERTAIN"])
def test_goal_plan_migration_retains_original_step_promise_and_evidence(tmp_path, state):
    path = tmp_path / "goals.sqlite3"
    migrate(path, "0044_voice_owner_authority")
    now = "2026-10-25T00:00:00+00:00"
    scope = encode({"kind": "read_content", "url": "https://example.com/original"})
    with closing(Store(path)) as store:
        store.insert("principals", actor_id="owner", kind="owner", created_at=now)
        store.insert("conversations", conversation_id="conversation", platform="qq.onebot",
            platform_session_key="owner-private", scope="private", owner_actor_id="owner",
            created_at=now)
        store.insert("events", event_id="original-event", schema_version=1, source="qq.onebot",
            source_event_id="original-native", conversation_id="conversation", actor_id="owner",
            event_type="message", occurred_at=now, ingested_at=now, privacy_scope="private",
            body_json='{"text":"original promise"}', trace_id="original-trace")
        store.insert("goals", goal_id="original-goal", kind="commitment", topic="original",
            scope_json=scope, proposal_key="original-proposal", state="RUNNING", priority=1,
            next_due_at=now, created_at=now, updated_at=now,
            authority_json=encode({"retained_original": "native-reference"}))
        store.insert("goal_steps", step_id="original-step", goal_id="original-goal", state=state,
            attempts=2, max_attempts=2, lease_token="original-lease", lease_until=now,
            captured_privacy_epoch=4, outcome_event_id="original-event", last_error_code="ORIGINAL",
            created_at=now, updated_at=now)
        store.insert("goal_evidence", goal_id="original-goal", event_id="original-event")
        for event_id in ("middle-source", "root-source"):
            store.insert("events", event_id=event_id, schema_version=1, source="qq.onebot",
                source_event_id=event_id, conversation_id="conversation", actor_id="owner",
                event_type="message", occurred_at=now, ingested_at=now, privacy_scope="private",
                body_json='{"text":"retained original source"}', trace_id=event_id)
        for event_id, source_id in (("original-event", "middle-source"),
            ("middle-source", "root-source"), ("original-event", "root-source"),
            ("root-source", "original-event")):
            store.insert("event_dependencies", event_id=event_id, source_event_id=source_id)
        store.insert("commitments", commitment_id="original-commitment", goal_id="original-goal",
            accepted_event_id="original-event", state="IN_PROGRESS", deadline=now,
            scope_sha256="a" * 64, created_at=now, updated_at=now)
        old_goal = store.one("SELECT * FROM goals")
        old_step = store.one("SELECT * FROM goal_steps")
        old_commitment = store.one("SELECT * FROM commitments")
        originals = {table: store.rows(f"SELECT * FROM {table} ORDER BY rowid") for table in (
            "goal_evidence", "event_dependencies", "events")}
    migrate(path)
    with closing(Store(path)) as store:
        migrated = store.one("SELECT * FROM goal_steps")
        assert {key: migrated[key] for key in old_step} == old_step
        assert migrated["position"] == 1 and migrated["scope_json"] == scope
        assert store.one("SELECT * FROM goals") == old_goal
        assert store.one("SELECT * FROM commitments") == old_commitment
        assert store.rows("SELECT * FROM goal_step_evidence ORDER BY event_id") == [
            {"step_id": "original-step", "event_id": key}
            for key in ("middle-source", "original-event", "root-source")]
        assert originals == {table: store.rows(f"SELECT * FROM {table} ORDER BY rowid")
                             for table in originals}
        assert store.schema_capabilities()
        assert store.rows("PRAGMA foreign_key_check") == []
        store.insert("goal_steps", step_id="next-step", goal_id="original-goal", position=2,
            scope_json=scope, state="WAITING_DECISION", max_attempts=1,
            created_at=now, updated_at=now)
        for position in (2, 7, 1.5):
            with pytest.raises(sqlite3.IntegrityError):
                store.insert("goal_steps", step_id="forbidden-step", goal_id="original-goal",
                    position=position, scope_json=scope, state="WAITING_DECISION", max_attempts=1,
                    created_at=now, updated_at=now)
        store.insert("goal_steps", step_id="third-step", goal_id="original-goal", position=3,
            scope_json=scope, state="WAITING_DECISION", max_attempts=1,
            created_at=now, updated_at=now)
        with store.transaction():
            store.execute("UPDATE goal_steps SET position=position+3")
            assert [r["position"] for r in store.rows(
                "SELECT position FROM goal_steps ORDER BY position")] == [4, 5, 6]
            with pytest.raises(sqlite3.IntegrityError, match="GOAL_STEP_LIMIT"):
                store.insert("goal_steps", step_id="fourth-step", goal_id="original-goal",
                    position=1, scope_json=scope, state="WAITING_DECISION", max_attempts=1,
                    created_at=now, updated_at=now)
            store.execute("UPDATE goal_steps SET position=position-3")
        # An interrupted reorder rolls back its parking positions and original IDs.
        before = store.rows("SELECT * FROM goal_steps ORDER BY position")
        with pytest.raises(RuntimeError, match="interrupted"):
            with store.transaction():
                store.execute("UPDATE goal_steps SET position=position+3")
                raise RuntimeError("interrupted")
        assert store.rows("SELECT * FROM goal_steps ORDER BY position") == before
        store.insert("goals", **(old_goal | {"goal_id": "another-goal",
            "proposal_key": "another-proposal"}))
        store.insert("goal_steps", step_id="another-step", goal_id="another-goal", position=4,
            scope_json=scope, state="WAITING_DECISION", max_attempts=1,
            created_at=now, updated_at=now)
        with pytest.raises(sqlite3.IntegrityError, match="GOAL_STEP_LIMIT"):
            store.execute("UPDATE goal_steps SET goal_id='original-goal' "
                          "WHERE step_id='another-step'")
        store.execute("UPDATE goal_steps SET goal_id=goal_id WHERE goal_id='original-goal'")
        guarded = historical_database(store, tmp_path / "goal-guard.sqlite3", "0045_goal_read_plan")
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "migrations"))
    config.set_main_option("sqlalchemy.url", "sqlite:///" + guarded.as_posix())
    with pytest.raises(RuntimeError, match="GOAL_PLAN_DOWNGRADE_REQUIRES_EXPLICIT_CUSTODY"):
        command.downgrade(config, "0044_voice_owner_authority")
    with closing(Store(guarded)) as store:
        assert store.one("SELECT COUNT(*) n FROM goal_steps")["n"] == 4
        assert store.rows("PRAGMA foreign_key_check") == []
