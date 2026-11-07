from companion.life_protocol import LifeLoopConfig, LifeShadowApproval
from companion.store import after
from tests.conftest import ingest
from tests.support.reading_service import configure
from tests.support.social_owner_authority import authorize


def prepared(core, tmp_path, *, affect=True, text="明天可以看看这篇天文介绍",
             additional_purposes=(), automatic_actions=()):
    configure(core)
    core.settings.affect_enabled = True
    core.settings.life_loop = LifeLoopConfig(mode="SHADOW", read_goals_enabled=True)
    core.settings.wake_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    core.settings.content_enabled = False
    ingest(core, 1, text)
    ingest(core, 2, "简短一点")
    core.store.execute("UPDATE jobs SET state='CANCELLED',lease_token=NULL")
    core.settings.content_enabled = True
    core.life_topics.synchronize()
    topic = core.store.one("SELECT * FROM life_topics")
    scope = LifeShadowApproval(
        scope="owner_private_life_shadow_v1", authorization_sha256="c" * 64,
        conversation_id=topic["conversation_id"], source_event_ids=["evt_test_1", "evt_test_2"],
        purposes=["PRIVATE_MESSAGE_DRAFT", "PRIVATE_DIARY"], max_items=5,
        expires_at=after(core.clock(), 3 * 86400),
        automatic_actions=list(automatic_actions),
    )
    authorize(core, tmp_path, life=scope,
              additional_purposes=["goals", "content", "exploration", "memory",
                                   *additional_purposes,
                                   *(["affect"] if affect else [])])
    core.clock.advance(86400)
    return topic["topic_id"]
