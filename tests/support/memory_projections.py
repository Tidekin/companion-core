from tests.support.observation_capture import enable_observations, observation


def fetched(core, goal):
    enable_observations(core, "web")
    core.settings.web_reading_enabled = True
    core.settings.web_reading_hosts = ("example.org",)
    item = observation(core, source="web", source_event_ids=core.goals.source_ids(goal))
    core.observations.append("reader", item, reading=True)
    return item.event_id
