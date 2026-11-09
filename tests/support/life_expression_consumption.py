import copy

import pytest

from companion import paid_budget
from companion.life_protocol import (
    LifeConsumeRequest,
    LifeLoopConfig,
    LifeQZoneSourceGrant,
    LifeShadowApproval,
)
from companion.life_topics import _source
from companion.reading_protocol import ReadObservation
from companion.store import after, encode
from tests.conftest import run
from tests.support.reading_protocol import envelope
from tests.support.reading_service import configure
from tests.support.reply_revision import accept
from tests.support.social_autonomy import service
from tests.support.social_owner_authority import authorize


@pytest.fixture(autouse=True)
def close_provider_clients(core):
    core.authority_test_clients = []
    yield
    for client in core.authority_test_clients:
        run(client.aclose())


def prepared(core, tmp_path, *, destination="QZONE_DRAFT", private=False, purposes=None,
             execute=True, automatic_actions=(), social_options=None):
    configure(core)
    payload = envelope("bilibili")
    url = payload["observations"][0]["url"]
    accepted = accept(core, text="这是我的私人计划，之后再聊" if private else "请阅读 " + url,
                      origin="onebot-direct:FriendMessage:" + core.settings.owner_qq_id)
    if private:
        source_id = accepted["event_id"]
    else:
        existing = core.store.one(
            'SELECT request_id FROM exploration_requests ORDER BY '
            'record_sequence DESC'
        )
        if existing:
            request_id = existing["request_id"]
        else:
            with core.store.transaction():
                request_id = core.exploration.create("bili", "read", "life-public-material", url,
                                                      source_event_ids=[accepted["event_id"]])
        item = core.reading.lease("bili")
        assert item["request_id"] == request_id
        payload["lease_token"] = item["lease_token"]
        payload["observations"][0]["provenance"] = copy.deepcopy(item["context"])
        payload["observations"][0]["fetched_at"] = core.clock()
        payload["observations"][0]["evidence"]["fetched_at"] = core.clock()
        source_id = core.reading.complete("bili", request_id,
                                          ReadObservation.model_validate(payload))["event_ids"][0]
    # Retained input/read receipt fixture; no fabricated foreground reply or send.
    core.store.execute("UPDATE jobs SET state='CANCELLED',lease_token=NULL")
    core.settings.life_loop = LifeLoopConfig(mode="SHADOW")
    core.settings.wake_enabled = core.settings.proactive_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    service(core, max_output_tokens=512, **(social_options or {}))
    source = _source(core.store, core.settings, source_id, core.clock())
    grant = LifeQZoneSourceGrant(
        grant_id="draft-source-one", revision=1, purpose="qzone_draft", event_id=source_id,
        source_sha256=source["source_sha256"], expires_at=after(core.clock(), 3 * 86400),
        audience="owner_only", owner_qq_id=core.settings.owner_qq_id,
    )
    conversation = core.store.one('SELECT conversation_id FROM events WHERE event_id=%s',
                                  (accepted["event_id"],))["conversation_id"]
    scope = LifeShadowApproval(
        scope="owner_private_life_shadow_v1", authorization_sha256="c" * 64,
        conversation_id=conversation, source_event_ids=[source_id],
        purposes=["PRIVATE_MESSAGE_DRAFT", "PRIVATE_DIARY"], max_items=5,
        expires_at=after(core.clock(), 3 * 86400), qzone_draft_sources=[grant],
        automatic_actions=list(automatic_actions),
    )
    purposes = purposes if purposes is not None else ["social", "account_actions", "proactive"]
    policy, ref = authorize(core, tmp_path, life=scope, additional_purposes=purposes)
    core.model.activity = encode({
        "action": "SHARE", "destination": destination, "source_event_ids": [source_id],
        "reason": "公开阅读后的一个想法" if not private else "私人想法",
        "text": "这段关于恒星颜色的介绍让我想再弄懂光谱与温度的关系。",
    })
    core.clock.advance(86400 if private else core.settings.proactive_idle_seconds + 1)
    if not execute:
        return None, policy, ref, source_id
    job_id = core.life_topics.schedule()
    assert job_id
    assert run(core.run_once(job_id=job_id))
    result = core.store.one('SELECT * FROM life_activity_results WHERE job_id=%s', (job_id,))
    assert result["state"] == "SHADOW", core.store.rows("SELECT * FROM jobs")
    return job_id, policy, ref, source_id


def request(core, job_id, ref, action="qzone.publish"):
    text = core.store.one('SELECT text FROM life_activity_results WHERE job_id=%s',
                          (job_id,))["text"]
    return LifeConsumeRequest(authority=ref, action=action,
                              expected_content_sha256=paid_budget.sha({"text": text}))
