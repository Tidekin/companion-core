from tests.conftest import ingest
from tests.support.exploration import configure
from tests.support.heybox_reader import URL
from tests.support.observation_capture import observation


def source_with_older_parent(core):
    configure(core, social=True)
    core.settings.raw_event_retention_days = 2
    core.settings.proactive_enabled = True
    core.settings.proactive_idle_seconds = 0
    ingest(core, 1, "主人提供的阅读线索")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.clock.advance(86400)
    item = observation(
        core, source="heybox", source_event_ids=["evt_test_1"], text="有据的天文阅读正文",
        payload={"title": "天文进展", "text": "有据的天文阅读正文", "url": URL},
    )
    core.observations.append("heybox", item)
    core.clock.advance(86399)
    return item.event_id
