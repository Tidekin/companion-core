"""Read retained native plan rows; creation/continuation is verified by the Life domain."""

from fastapi.testclient import TestClient

from companion import owner_authority
from companion.api import create_app
from companion.cognition_read import CognitionRead
from companion.owner_authority_protocol import AuthorityStop
from companion.store import encode, uid
from tests.support.cognition_goal_steps import finish_record, retained_plan
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare


def read(core, goal):
    return CognitionRead(core).page("goal", record_id=goal)["items"]


def test_first_done_step_never_completes_parent_or_writes(core):
    goal, steps = retained_plan(core)
    finish_record(core, steps[0], 1)
    before = core.store.db.total_changes
    item = read(core, goal)[0]
    assert item["execution"]["state"] == "planned"
    assert [s["step_id"] for s in item["steps"]] == steps
    assert [s["state"] for s in item["steps"]] == ["DONE", "WAITING_DECISION", "WAITING_DECISION"]
    assert item["verified_completed_steps"] == 1
    assert "step" not in item and core.store.db.total_changes == before
    core.store.execute("UPDATE goals SET state='DONE' WHERE goal_id=?", (goal,))
    assert read(core, goal)[0]["execution"]["state"] == "UNKNOWN"


def test_parent_done_requires_all_real_results_and_exact_final_result(core):
    goal, steps = retained_plan(core)
    outcomes = [finish_record(core, step, i) for i, step in enumerate(steps, 1)]
    core.store.execute("UPDATE goals SET state='DONE',result_event_id=? WHERE goal_id=?",
                        (outcomes[-1], goal))
    item = read(core, goal)[0]
    assert item["execution"]["state"] == "done" and item["verified_completed_steps"] == 3
    core.store.execute("UPDATE observations SET result_status='UNKNOWN' WHERE event_id=?",
                        (outcomes[1],))
    assert read(core, goal)[0]["execution"]["state"] == "UNKNOWN"
    assert read(core, goal)[0]["steps"][1]["outcome_event_id"] is None


def test_later_step_uncertainty_survives_parent_cancellation(core):
    goal, steps = retained_plan(core)
    finish_record(core, steps[0], 1)
    core.store.execute("UPDATE goal_steps SET state='EXECUTION_UNCERTAIN' WHERE step_id=?",
                        (steps[2],))
    core.store.execute("UPDATE goals SET state='CANCELLED' WHERE goal_id=?", (goal,))
    assert read(core, goal)[0]["execution"]["state"] == "UNKNOWN"


def test_withdrawn_step_source_hides_plan_even_when_initial_roots_live(core):
    goal, steps = retained_plan(core)
    outcome = finish_record(core, steps[0], 1)
    core.store.insert("goal_step_evidence", step_id=steps[1], event_id=outcome)
    assert read(core, goal)
    with core.store.transaction():
        core.ledger.expire_events(core.store, [outcome], core.clock())
    assert not read(core, goal)


def test_actual_owner_api_reads_steps_without_execution_permission(core):
    goal, steps = retained_plan(core)
    finish_record(core, steps[0], 1)
    # Turning execution off must not hide valid historical metadata.
    core.settings.goals_enabled = False
    with TestClient(create_app(core.settings)) as client:
        client.app.state.core.clock = core.clock
        selection = {"kind": "goal", "record_id": goal}
        bridge = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
        denied = client.post("/internal/v2/cognition", json=selection, headers=bridge)
        assert denied.status_code == 401
        owner = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
        response = client.post("/internal/v2/cognition", json=selection, headers=owner)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert len(response.json()["items"][0]["steps"]) == 3
        assert response.json()["items"][0]["execution"]["state"] == "planned"


def test_original_receipts_are_per_step_bounded_and_prioritize_unknown(core):
    goal, steps = retained_plan(core)
    unknown_id = None
    for number in range(23):
        request = uid("read")
        if number == 22:
            unknown_id = request
        core.store.insert("exploration_requests", request_id=request, request_key=request,
            adapter_id="reader", operation="read", url="https://example.org/article",
            goal_step_json=encode({"goal_id": goal, "step_id": steps[1]}),
            state="UNKNOWN" if number == 22 else "DONE", captured_privacy_epoch=0,
            created_at=core.clock(), updated_at=core.clock())
        core.store.insert("exploration_evidence", request_id=request, event_id="evt_test_1")
    before = core.store.rows("SELECT * FROM exploration_requests")
    item = read(core, goal)[0]
    assert item["steps"][0]["execution"]["receipts"] == []
    selected = item["steps"][1]["execution"]
    assert selected["state"] == item["execution"]["state"] == "UNKNOWN"
    assert selected["truncated"] and len(selected["receipts"]) == 20
    assert selected["receipts"][0]["request_id"] == unknown_id
    assert {r["step_id"] for r in selected["receipts"]} == {steps[1]}
    assert core.store.rows("SELECT * FROM exploration_requests") == before
    core.store.execute("DELETE FROM exploration_evidence")
    item = read(core, goal)[0]
    assert item["execution"]["state"] == "UNKNOWN"
    assert item["steps"][1]["execution"] == {
        "state": "UNKNOWN", "receipts": [], "truncated": True}


def test_stop_does_not_remove_valid_historical_steps(core, tmp_path):
    goal, steps = retained_plan(core)
    finish_record(core, steps[0], 1)
    grant, _ = activate(core, prepare(core, tmp_path))
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="cognition-plan-stop", authority=grant["authority"]))
    assert read(core, goal)[0]["verified_completed_steps"] == 1


def test_committed_temporary_reorder_positions_are_not_a_valid_visible_plan(core):
    goal, steps = retained_plan(core)
    core.store.execute("UPDATE goal_steps SET position=4 WHERE step_id=?", (steps[0],))
    item = read(core, goal)[0]
    assert item["execution"]["state"] == "UNKNOWN"
    assert all(step["execution"]["state"] == "UNKNOWN" for step in item["steps"])


def test_three_completed_steps_with_last_parked_position_cannot_report_completion(core):
    goal, steps = retained_plan(core)
    outcomes = [finish_record(core, step, i) for i, step in enumerate(steps, 1)]
    core.store.execute("UPDATE goals SET state='DONE',result_event_id=? WHERE goal_id=?",
                        (outcomes[-1], goal))
    assert read(core, goal)[0]["execution"]["state"] == "done"
    core.store.execute("UPDATE goal_steps SET position=4 WHERE step_id=?", (steps[-1],))
    before = core.store.db.total_changes
    item = read(core, goal)[0]
    assert item["execution"]["state"] == "UNKNOWN"
    assert item["verified_completed_steps"] == 0
    assert [step["position"] for step in item["steps"]] == [1, 2, 4]
    assert all(step["execution"]["state"] == "UNKNOWN" for step in item["steps"])
    assert core.store.db.total_changes == before
