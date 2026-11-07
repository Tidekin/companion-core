from companion.store import encode, uid
from tests.support.goals import setup_goal
from tests.support.observation_capture import enable_observations, observation


def retained_plan(core):
    goal = setup_goal(core)
    core.store.execute("UPDATE goals SET state='WAITING_INPUT',scope_json=%s WHERE goal_id=%s",
        (encode({"operation": "read_plan", "urls": [f"https://example.org/{i}" for i in range(3)]}),
         goal))
    steps = []
    for position in range(1, 4):
        step = uid("step")
        core.store.insert("goal_steps", step_id=step, goal_id=goal, position=position,
            scope_json=encode({"operation": "read_content", "url": f"https://example.org/{position}"}),
            state="WAITING_DECISION", max_attempts=3,
            created_at=core.clock(), updated_at=core.clock())
        core.store.insert("goal_step_evidence", step_id=step, event_id="evt_test_1")
        steps.append(step)
    return goal, steps


def finish_record(core, step, number):
    enable_observations(core, "web")
    core.settings.web_reading_enabled = True
    core.settings.web_reading_hosts = ("example.org",)
    roots = [r["event_id"] for r in core.store.rows(
        'SELECT event_id FROM goal_step_evidence WHERE step_id=%s', (step,))]
    item = observation(core, number, source="web", source_event_ids=roots,
                       text="retained real-source observation " + str(number))
    core.observations.append("reader", item, reading=True)
    core.store.execute("UPDATE goal_steps SET state='DONE',outcome_event_id=%s WHERE step_id=%s",
                        (item.event_id, step))
    return item.event_id
