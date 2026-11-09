from datetime import datetime

from companion.goals import CommitmentRequest, GoalProposal, GoalScope
from companion.life_protocol import LifeLoopConfig
from companion.store import after
from tests.conftest import ingest
from tests.support.goals import result_for
from tests.support.observation_capture import enable_observations

ASTRONOMY, BAKING = "天文研究", "烘焙尝试"


def prepare(core):
    core.settings.goals_enabled = core.settings.exploration_enabled = True
    core.settings.wake_enabled = core.settings.memory_enabled = True
    core.settings.watch_ledger = core.settings.watch_ledger.model_copy(update={"enabled": True})
    core.settings.life_loop = LifeLoopConfig(autonomy_enabled=True)
    enable_observations(core)


def read(core, number, topic, url, *, said="想找点东西看看", commitment=False):
    """One real chain: owner message -> native goal -> leased step -> Reader receipt."""
    ingest(core, number, said)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE event_id=%s",
                       (f"evt_test_{number}",))
    proposal = GoalProposal(topic=topic, source_event_ids=[f"evt_test_{number}"],
                            scope=GoalScope(operation="read_content", url=url))
    if commitment:
        goal = core.goals.accept_commitment(CommitmentRequest(
            proposal=proposal, accepted_event_id=f"evt_test_{number}",
            deadline=datetime.fromisoformat(after(core.clock(), 86400))))["goal_id"]
    else:
        goal = core.goals.propose(proposal)
        core.goals.accept(goal)
    step = core.goals.claim()
    core.goals.finish(step, result_for(core, goal, number))
    return goal, f"obs_test_{number}"


def judge(core, event_id, rating, take):
    """A settled Watch Ledger judgement; the paid path itself is covered below."""
    entry_id = "watch_" + event_id
    core.store.execute(
        (
            'INSERT INTO watch_ledger(entry_id,source_event_id,platform,item_'
            'ref,depth,state,rating,take,topic_tags_json,created_at,updated_a'
            't) SELECT '
            "%s,event_id,'web',canonical_url,'text','JUDGED',%s,%s,'[]',%s,%s"
            ' FROM observations WHERE event_id=%s'
        ),
        (entry_id, rating, take, core.clock(), core.clock(), event_id))
    return entry_id
