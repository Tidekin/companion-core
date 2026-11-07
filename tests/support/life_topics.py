from companion.life_protocol import LifeLoopConfig
from companion.store import encode
from tests.conftest import ingest


def configure(core, mode="SHADOW"):
    core.settings.life_loop = LifeLoopConfig(mode=mode, autonomy_enabled=True)
    core.settings.wake_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0


def bug(core):
    configure(core)
    ingest(core, 1, "导出时还有一个没有解决的Bug，今天先到这里")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.life_topics.synchronize()
    return core.store.one("SELECT * FROM life_topics")


def proposal(core, action="FOLLOW_UP", destination="PRIVATE_MESSAGE_DRAFT", **extra):
    core.model.activity = encode(
        {
            "action": action,
            "destination": destination,
            "source_event_ids": ["evt_test_1"],
            "reason": "昨日未结束的话题",
            "text": "昨天那个导出问题有新线索了吗？有空再聊就好。",
            **extra,
        }
    )


def due(core):
    core.clock.advance(86400)
    return core.life_topics.schedule()


def assert_no_delivery(core):
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.store.rows("SELECT * FROM social_autonomy_plans") == []
