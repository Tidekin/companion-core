import json
from datetime import datetime

from companion.goals import CommitmentRequest, GoalProposal, GoalScope
from companion.store import after
from tests.conftest import ingest
from tests.support.observation_capture import enable_observations, observation


def setup_goal(
    core, number=1, commitment=False, topic="了解天文内容", url="https://example.org/article"
):
    core.settings.goals_enabled = core.settings.exploration_enabled = True
    core.settings.wake_enabled = True
    enable_observations(core)
    ingest(core, number, "请阅读这篇天文文章")
    core.store.execute(
        "UPDATE jobs SET state='SUCCEEDED' WHERE event_id=?", (f"evt_test_{number}",)
    )
    proposal = GoalProposal(
        topic=topic,
        source_event_ids=[f"evt_test_{number}"],
        scope=GoalScope(operation="read_content", url=url),
    )
    if commitment:
        return core.goals.accept_commitment(
            CommitmentRequest(
                proposal=proposal,
                accepted_event_id=f"evt_test_{number}",
                deadline=datetime.fromisoformat(after(core.clock(), 86400)),
            )
        )["goal_id"]
    return core.goals.propose(proposal)


def result_for(core, goal_id, number=1, **changes):
    text = f"实际获得的天文内容 {number}"
    item = observation(
        core,
        number,
        text=text,
        source_event_ids=core.goals.source_ids(goal_id),
        payload={"text": text, "url": json.loads(core.goals.get(goal_id)["scope_json"])["url"]},
        **changes,
    )
    core.observations.append("reader", item)
    return item.event_id
