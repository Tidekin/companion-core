"""Native finite read plans use original authority, steps, receipts and erasure."""

import asyncio
import json
import sqlite3
import sys
from datetime import datetime

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.goals import GoalContinuationDecision, GoalProposal, GoalScope
from companion.memory_projections import MemoryProjections
from companion.store import after
from tests.conftest import run
from tests.support.goal_read_plan import URLS, create, prepared, read


def decision(core, goal_id, *, action="CONTINUE", order=None, **extra):
    snapshot = core.goals.plan(goal_id)
    return GoalContinuationDecision(expected_version=snapshot["version"], action=action,
        step_order=order if order is not None else [step["step_id"] for step in snapshot["steps"]
            if step["state"] == "WAITING_DECISION"] if action == "CONTINUE" else [], **extra)


def test_existing_scope_serialization_and_hash_shape_is_unchanged():
    assert GoalScope().model_dump() == {"operation": "investigate", "url": ""}
    assert GoalScope(operation="read_content", url=URLS[0]).model_dump() == {
        "operation": "read_content", "url": URLS[0]}
    for value in ([], [URLS[0], URLS[0]], [*URLS, URLS[0]], ["http://example.org"],
                  ["https://user:password@example.org"], ["https://:password@example.org"]):
        with pytest.raises(ValidationError):
            GoalScope(operation="read_plan", urls=value)


def test_plan_serial_receipts_reorder_cas_and_original_evidence(core, tmp_path):
    async def scenario():
        async with prepared(core, tmp_path) as (client, source, _):
            goal_id = await create(client, source)
            original = core.goals.get(goal_id)
            initial = core.goals.plan(goal_id)
            assert [step["state"] for step in initial["steps"]] == [
                "QUEUED", "WAITING_DECISION", "WAITING_DECISION"]
            assert core.exploration.schedule_goal()
            assert not core.exploration.schedule_goal()
            assert await read(client, core.clock(), URLS[0]) == (True, 3)
            assert core.goals.get(goal_id)["state"] == "WAITING_INPUT"
            first_view = await client.post("/internal/v2/cognition",
                json={"kind": "goal", "record_id": goal_id})
            assert first_view.status_code == 200, first_view.text
            assert first_view.json()["items"][0]["verified_completed_steps"] == 1
            assert first_view.json()["items"][0]["execution"]["state"] == "planned"
            snapshot = core.goals.plan(goal_id)
            first_result = snapshot["steps"][0]["outcome_event_id"]
            assert first_result and not core.exploration.schedule_goal()
            pending = list(reversed([step["step_id"] for step in snapshot["steps"][1:]]))
            selected = decision(core, goal_id, order=pending)
            core.goals.continue_plan(goal_id, selected, autonomous=True)
            with pytest.raises(CoreError, match="GOAL_VERSION_CHANGED"):
                core.goals.continue_plan(goal_id, selected, autonomous=True)
            assert first_result in core.goals.step_source_ids(pending[0])
            assert core.goals.source_ids(goal_id) == [source]
            assert core.goals.get(goal_id)["scope_json"] == original["scope_json"]
            for target in (URLS[2], URLS[1]):
                assert core.exploration.schedule_goal()
                assert await read(client, core.clock(), target) == (True, 3)
                if target == URLS[2]:
                    assert core.goals.get(goal_id)["state"] == "WAITING_INPUT"
                    core.goals.continue_plan(goal_id, decision(core, goal_id), autonomous=True)
            final = core.goals.plan(goal_id)
            assert final["state"] == "DONE"
            assert all(step["state"] == "DONE" for step in final["steps"])
            assert len(core.store.rows("SELECT * FROM reading_attempts")) == 3
            assert len({step["outcome_event_id"] for step in final["steps"]}) == 3
            eid = core.memory.episodes(core.store.rows("SELECT * FROM events WHERE event_id=?",
                                                       (source,)), core.clock())
            projected = MemoryProjections(core.memory).snapshot(core.clock(), episode_ids=[eid])
            assert projected["episodes"][0]["goals"][0]["state"] == "done"
            assert len(projected["episodes"][0]["goals"][0]["steps"]) == 3
            assert projected["episodes"][0]["temperature"] == "Warm"
            final_view = await client.post("/internal/v2/cognition",
                json={"kind": "goal", "record_id": goal_id})
            assert final_view.status_code == 200, final_view.text
            assert final_view.json()["items"][0]["verified_completed_steps"] == 3
            assert final_view.json()["items"][0]["execution"]["state"] == "done"
    run(scenario())


def test_wait_and_abandon_preserve_completed_identity_and_commitments(core, tmp_path):
    async def scenario():
        async with prepared(core, tmp_path) as (client, source, _):
            proposal = GoalProposal(topic="多步承诺不自动接受", source_event_ids=[source],
                                    scope=GoalScope(operation="read_plan", urls=URLS))
            with pytest.raises(CoreError, match="COMMITMENT_SINGLE_TARGET_REQUIRED"):
                core.goals.propose(proposal, kind="commitment")
            goal_id = await create(client, source, count=2)
            assert core.exploration.schedule_goal()
            assert await read(client, core.clock(), URLS[0]) == (True, 3)
            completed = core.store.one("SELECT * FROM goal_steps WHERE state='DONE'")
            core.goals.continue_plan(goal_id, decision(core, goal_id, action="WAIT",
                wait_until=datetime.fromisoformat(after(core.clock(), 60))))
            assert not core.exploration.schedule_goal()
            with pytest.raises(CoreError, match="GOAL_WAIT_NOT_DUE"):
                core.goals.continue_plan(goal_id, decision(core, goal_id))
            core.goals.continue_plan(goal_id, decision(core, goal_id, action="ABANDON"))
            assert core.goals.get(goal_id)["state"] == "CANCELLED"
            assert core.store.one("SELECT * FROM goal_steps WHERE state='DONE'") == completed
            assert len(core.store.rows("SELECT * FROM reading_attempts")) == 1
    run(scenario())


def test_unknown_dispatch_never_replays_or_continues_after_restart(core, tmp_path):
    async def scenario():
        async with prepared(core, tmp_path) as (client, source, _):
            goal_id = await create(client, source, count=2)
            assert core.exploration.schedule_goal()
            assert core.reading.lease("bili")  # Simulate process loss after native claim.
            core.clock.advance(core.settings.lease_seconds + 1)
            core.exploration.recover()
            original = core.store.rows("SELECT * FROM exploration_requests")
            assert original[0]["state"] == "UNKNOWN"
            assert core.goals.plan(goal_id)["unknown"]
            with Core(core.settings, core.model, core.clock) as reopened:
                assert not reopened.exploration.schedule_goal()
                with pytest.raises(CoreError, match="GOAL_EXECUTION_UNCERTAIN"):
                    reopened.goals.continue_plan(goal_id, decision(reopened, goal_id))
                assert reopened.store.rows("SELECT * FROM exploration_requests") == original
    run(scenario())


def test_prior_result_erasure_cancels_tail_and_replays_into_old_backup(core, tmp_path):
    async def scenario():
        async with prepared(core, tmp_path) as (client, source, _):
            goal_id = await create(client, source, count=2)
            assert core.exploration.schedule_goal()
            assert await read(client, core.clock(), URLS[0]) == (True, 3)
            core.goals.continue_plan(goal_id, decision(core, goal_id))
            result = core.store.one("SELECT outcome_event_id FROM goal_steps WHERE state='DONE'")[
                "outcome_event_id"]
            archive, target = tmp_path / "plan.enc", tmp_path / "restore.db"
            key = Fernet.generate_key()
            backup(core.settings.database, archive, key)
            with core.store.transaction():
                core.ledger.expire_events(core.store, [result], core.clock())
            restore(archive, target, key, core.settings.erasure_ledger)
            with Core(core.settings.model_copy(update={"database": str(target)}),
                      core.model, core.clock) as restored:
                for instance in (core, restored):
                    assert instance.store.one("SELECT redacted FROM goals WHERE goal_id=?",
                        (goal_id,))["redacted"]
                    assert not instance.store.rows("SELECT * FROM goal_step_evidence")
                    assert not instance.store.rows(
                        "SELECT * FROM goal_steps WHERE scope_json<>'{}'")
    run(scenario())


def test_real_goal_http_and_independent_reader_complete_two_step_plan(core, tmp_path):
    async def tick(client, url):
        process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
            "tests.goal_plan_reader_process", "--base", str(client.base_url),
            "--now", core.clock(), "--url", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            async with asyncio.timeout(45):
                output, error = await process.communicate()
            assert process.returncode == 0, (output, error)
            assert json.loads(output.decode().splitlines()[-1]) == {"worked": True, "calls": 3}
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def scenario():
        async with prepared(core, tmp_path) as (client, source, _):
            goal_id = await create(client, source, count=2)
            assert core.exploration.schedule_goal()
            await tick(client, URLS[0])
            step = decision(core, goal_id)
            path = f"/internal/v2/goals/{goal_id}/continuation"
            before = core.store.rows("SELECT * FROM goal_steps ORDER BY step_id")
            denied = await client.post(path, json=step.model_dump(mode="json"), headers={
                "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()})
            assert denied.status_code == 401
            assert core.store.rows("SELECT * FROM goal_steps ORDER BY step_id") == before
            reply = await client.post(f"/internal/v2/goals/{goal_id}/continuation",
                                      json=step.model_dump(mode="json"))
            assert reply.status_code == 200, reply.text
            stale = await client.post(path, json=step.model_dump(mode="json"))
            assert stale.status_code == 409 and stale.json()["error"] == "GOAL_VERSION_CHANGED"
            assert core.exploration.schedule_goal()
            await tick(client, URLS[1])
            assert core.goals.get(goal_id)["state"] == "DONE"
    run(scenario())


@pytest.mark.parametrize("change", ["revision", "stop", "order", "replayed_result"])
def test_continuation_cannot_upgrade_authority_or_reuse_another_step_receipt(
    core, tmp_path, change,
):
    from companion import owner_authority
    from companion.owner_authority_protocol import AuthorityStop
    from tests.support.owner_authority import activate

    async def scenario():
        async with prepared(core, tmp_path) as (client, source, reference):
            goal_id = await create(client, source, count=2)
            assert core.exploration.schedule_goal()
            assert await read(client, core.clock(), URLS[0]) == (True, 3)
            action = decision(core, goal_id)
            before = core.store.rows("SELECT * FROM exploration_requests")
            if change == "revision":
                policy = owner_authority.current(core.store, core.settings, core.clock).policy
                stopped = owner_authority.stop(core.store, core.settings, core.clock,
                    AuthorityStop(operation_id="quiesce-read-plan", authority=reference))
                activate(core, policy, revision=stopped["authority"]["revision"],
                         operation="replace-read-plan-authority")
            elif change == "stop":
                owner_authority.stop(core.store, core.settings, core.clock,
                    AuthorityStop(operation_id="stop-read-plan", authority=reference))
            elif change == "order":
                action = action.model_copy(update={"step_order": ["unrelated-step"]})
            else:
                previous = core.store.one(
                    "SELECT outcome_event_id FROM goal_steps WHERE state='DONE'")
                core.goals.continue_plan(goal_id, action)
                step = core.goals.claim()
                with pytest.raises(CoreError, match="GOAL_RESULT_REQUEST_MISMATCH"):
                    core.goals.finish(step, previous["outcome_event_id"])
                assert core.goals.get(goal_id)["state"] == "RUNNING"
                return
            with pytest.raises(CoreError):
                core.goals.continue_plan(goal_id, action, autonomous=True)
            assert core.store.rows("SELECT * FROM exploration_requests") == before
    run(scenario())


def test_reader_process_loss_after_dispatch_retains_unknown_without_new_request(core, tmp_path):
    async def child(client, *, crash=False):
        process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
            "tests.goal_plan_reader_process", "--base", str(client.base_url),
            "--now", core.clock(), "--url", URLS[0], *(["--crash"] if crash else []),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            async with asyncio.timeout(45):
                output, error = await process.communicate()
            assert process.returncode == (23 if crash else 0), (output, error)
            return output.decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def scenario():
        async with prepared(core, tmp_path) as (client, source, _):
            goal_id = await create(client, source, count=2)
            assert core.exploration.schedule_goal()
            assert "provider-dispatched" in await child(client, crash=True)
            core.clock.advance(core.settings.lease_seconds + 1)
            core.exploration.recover()
            before = core.store.rows("SELECT * FROM exploration_requests")
            assert before[0]["state"] == "UNKNOWN" and before[0]["reserved_requests"] > 0
            assert core.goals.plan(goal_id)["unknown"]
            assert not core.exploration.schedule_goal()
            assert json.loads((await child(client)).splitlines()[-1]) == {
                "worked": False, "calls": 0}
            assert core.store.rows("SELECT * FROM exploration_requests") == before
    run(scenario())


def test_reorder_sql_failure_rolls_back_parking_positions_and_step_identities(core, tmp_path):
    async def scenario():
        async with prepared(core, tmp_path) as (client, source, _):
            goal_id = await create(client, source)
            assert core.exploration.schedule_goal()
            assert await read(client, core.clock(), URLS[0]) == (True, 3)
            before = core.store.rows("SELECT * FROM goal_steps ORDER BY step_id")
            parent = core.goals.get(goal_id)
            order = list(reversed([step["step_id"] for step in core.goals.plan(goal_id)["steps"]
                                   if step["state"] == "WAITING_DECISION"]))
            core.store.execute("CREATE TEMP TRIGGER fail_position_refill BEFORE UPDATE OF position "
                "ON goal_steps WHEN OLD.position>3 AND NEW.position<=3 BEGIN "
                "SELECT RAISE(ABORT,'synthetic_position_refill_failure'); END")
            try:
                with pytest.raises(sqlite3.IntegrityError,
                                   match="synthetic_position_refill_failure"):
                    core.goals.continue_plan(goal_id, decision(core, goal_id, order=order))
            finally:
                core.store.execute("DROP TRIGGER fail_position_refill")
            assert core.store.rows("SELECT * FROM goal_steps ORDER BY step_id") == before
            assert core.goals.get(goal_id) == parent
            assert not core.store.one("SELECT 1 FROM goal_steps WHERE position>3")
    run(scenario())


def test_completed_plan_projection_requires_final_result_and_committed_positions(core, tmp_path):
    async def scenario():
        async with prepared(core, tmp_path) as (client, source, _):
            goal_id = await create(client, source)
            for index, url in enumerate(URLS):
                assert core.exploration.schedule_goal()
                assert await read(client, core.clock(), url) == (True, 3)
                if index < 2:
                    core.goals.continue_plan(goal_id, decision(core, goal_id))
            steps = core.store.rows("SELECT * FROM goal_steps WHERE goal_id=? ORDER BY position",
                                   (goal_id,))
            assert len(steps) == 3 and all(step["state"] == "DONE" for step in steps)
            eid = core.memory.episodes(core.store.rows("SELECT * FROM events WHERE event_id=?",
                                                       (source,)), core.clock())
            view = MemoryProjections(core.memory)
            assert view.snapshot(core.clock(), episode_ids=[eid])["episodes"][0]["goals"][0][
                "state"] == "done"
            retained = {table: core.store.rows(f"SELECT * FROM {table}") for table in (
                "memories", "model_usage", "model_admissions", "exploration_requests",
                "reading_attempts", "observations")}
            for damage in ("first_result", "parking_position"):
                core.store.execute("UPDATE goals SET result_event_id=? WHERE goal_id=?",
                    (steps[0 if damage == "first_result" else -1]["outcome_event_id"], goal_id))
                if damage == "parking_position":
                    core.store.execute("UPDATE goal_steps SET position=4 WHERE step_id=?",
                                       (steps[-1]["step_id"],))
                before = core.store.db.total_changes
                snapshot = view.snapshot(core.clock(), episode_ids=[eid])
                projected = snapshot["episodes"][0]["goals"][0]
                assert projected["native_state"] == "DONE" and projected["state"] == "UNKNOWN"
                assert not projected["closed"]
                assert core.store.db.total_changes == before
                assert retained == {table: core.store.rows(f"SELECT * FROM {table}")
                                    for table in retained}
    run(scenario())
