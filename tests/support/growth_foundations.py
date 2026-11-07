from companion.interest import profile
from tests.support.goals import result_for, setup_goal


def view(core):
    return profile(core.store, core.settings, core.clock(), core.settings.owner_qq_id)


def complete_read(core, number, url):
    goal = setup_goal(core, number, topic="天文研究", url=url)
    core.goals.accept(goal)
    step = core.goals.claim()
    core.goals.finish(step, result_for(core, goal, number))
    return goal
