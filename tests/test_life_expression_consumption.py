"""Source-bound Life wording reaches native channel custody without a second model."""

import copy
import json

import pytest

from companion import paid_budget, social_autonomy_runtime
from companion.contracts import CoreError
from companion.life_protocol import (
    LifeConsumeRequest,
    LifeLoopConfig,
    LifeQZoneSourceGrant,
    LifeShadowApproval,
)
from companion.life_topics import _source
from companion.reading_protocol import ReadObservation
from companion.reply_segments import SegmentedReplyConfig
from companion.social import proactive_counts
from companion.social_autonomy import SocialAutonomy
from companion.store import after, encode, uid
from tests.conftest import run
from tests.support.owner_authority import activate
from tests.test_reading_protocol import envelope
from tests.test_reading_service import configure
from tests.test_reply_revision import accept
from tests.test_social_autonomy import service
from tests.test_social_owner_authority import authorize, stop


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
        existing = core.store.one("SELECT request_id FROM exploration_requests ORDER BY rowid DESC")
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
    conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
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
    result = core.store.one("SELECT * FROM life_activity_results WHERE job_id=?", (job_id,))
    assert result["state"] == "SHADOW", core.store.rows("SELECT * FROM jobs")
    return job_id, policy, ref, source_id


def request(core, job_id, ref, action="qzone.publish"):
    text = core.store.one("SELECT text FROM life_activity_results WHERE job_id=?",
                          (job_id,))["text"]
    return LifeConsumeRequest(authority=ref, action=action,
                              expected_content_sha256=paid_budget.sha({"text": text}))


def test_private_life_segments_preserve_the_retained_draft_and_recheck_every_part(core, tmp_path):
    from companion import notices

    core.settings.segmented_reply = SegmentedReplyConfig(
        mode="semantic", max_segments="unlimited", interval_ms=600)
    _, _, ref, _ = prepared(core, tmp_path, destination="PRIVATE_MESSAGE_DRAFT", execute=False)
    proposal = json.loads(core.model.activity)
    proposal["text"] = "恒星的颜色真有意思。\n\n想再看看光谱。\n\n温度的关系也值得聊。"
    core.model.activity = encode(proposal)
    job_id = core.life_topics.schedule()
    assert run(core.run_once(job_id=job_id))
    body = request(core, job_id, ref, "qq.private.proactive")
    usage = core.store.rows("SELECT * FROM model_usage")
    consumed = core.life_topics.consume(job_id, body)
    assert len(consumed["deliveries"]) == 3
    assert core.life_topics.consume(job_id, body) == consumed
    rows = core.store.rows("SELECT * FROM outbox ORDER BY segment_index")
    assert "".join(json.loads(row["content_json"])["text"] for row in rows) == proposal["text"]
    assert proactive_counts(core.store, core.settings, core.clock())[0] == 1
    for row in rows:
        assert notices.delivery_problem(core.store, core.settings, row["message_id"],
                                        core.clock()) is None
    # A matching per-message digest must not hide a changed member of the frozen draft.
    changed = {"text": "被替换的尾段"}
    core.store.execute("UPDATE outbox SET content_json=?,content_sha256=? WHERE message_id=?",
                       (encode(changed), paid_budget.sha(changed), rows[-1]["message_id"]))
    assert notices.delivery_problem(core.store, core.settings, rows[0]["message_id"],
                                    core.clock()) is not None
    assert core.store.rows("SELECT * FROM model_usage") == usage


def test_public_life_draft_reuses_wording_and_native_outbox_once(core, tmp_path):
    job_id, _, ref, source = prepared(core, tmp_path)
    usage = core.store.rows("SELECT * FROM model_usage")
    calls = list(core.model.calls)
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.store.rows("SELECT * FROM social_autonomy_plans") == []
    assert proactive_counts(core.store, core.settings, core.clock()) == (0, False)
    assert not run(social_autonomy_runtime.run_once(
        core.store, core.settings, core.model, core.clock,
    ))
    assert core.model.calls == calls
    body = request(core, job_id, ref)
    consumed = core.life_topics.consume(job_id, body)
    assert consumed["state"] == "READY" and consumed["model_request_id"] is None
    assert consumed["plan"]["audience"] == {"ugc_right": 16, "target_uins": ["10001"]}
    assert consumed["plan"]["proposal"]["source_refs"] == [source]
    assert consumed["plan"]["proposal"]["text"] == core.life_topics.retained_result(job_id)[
        "result"
    ]["text"]
    assert core.life_topics.consume(job_id, body) == consumed
    assert len(core.store.rows("SELECT * FROM outbox")) == 1
    assert core.store.rows("SELECT * FROM model_usage") == usage and core.model.calls == calls


def test_exact_private_qzone_preview_grant_never_authorizes_publication(core, tmp_path):
    job_id, _, ref, _ = prepared(core, tmp_path, private=True)
    assert core.life_topics.retained_result(job_id)["qzone_draft"]
    with pytest.raises(CoreError, match="SOCIAL_PUBLIC_SOURCE_REQUIRED"):
        core.life_topics.consume(job_id, request(core, job_id, ref))
    assert core.store.rows("SELECT * FROM social_autonomy_plans") == []
    assert core.store.rows("SELECT * FROM outbox") == []


def test_qzone_publish_requires_independent_account_purpose(core, tmp_path):
    job_id, _, ref, _ = prepared(core, tmp_path, purposes=["social"])
    with pytest.raises(CoreError, match="OWNER_AUTHORITY_PURPOSE_REQUIRED"):
        core.life_topics.consume(job_id, request(core, job_id, ref))
    assert core.store.rows("SELECT * FROM social_autonomy_plans") == []


@pytest.mark.parametrize("mutation", ["body", "source", "fiction", "revision", "stop"])
def test_consumption_revalidates_exact_draft_and_native_authority(core, tmp_path, mutation):
    job_id, policy, ref, source = prepared(core, tmp_path)
    body = request(core, job_id, ref)
    if mutation == "body":
        core.store.execute("UPDATE life_activity_results SET text='modified'")
    elif mutation == "source":
        with core.store.transaction():
            core.ledger.expire_events(core.store, [source], core.clock())
    elif mutation == "fiction":
        core.store.execute("UPDATE life_activity_results SET proposal_json="
                           "json_set(proposal_json,'$.fiction',json('true'))")
    elif mutation == "revision":
        activate(core, policy, revision=1, operation="revise-before-consume")
    else:
        stop(core, ref)
    usage = core.store.rows("SELECT * FROM model_usage")
    with pytest.raises(CoreError):
        core.life_topics.consume(job_id, body)
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.store.rows("SELECT * FROM social_autonomy_plans") == []
    assert core.store.rows("SELECT * FROM model_usage") == usage


@pytest.mark.parametrize("mutation", ["erase", "stop", "draft_changed", "off", "quiet"])
def test_consumed_public_plan_rechecks_before_native_dispatch(core, tmp_path, mutation):
    job_id, _, ref, source = prepared(core, tmp_path)
    consumed = core.life_topics.consume(job_id, request(core, job_id, ref))
    message_id = consumed["delivery"]["message_id"]
    token = uid("lease")
    module = SocialAutonomy(core.store, core.settings, clock=core.clock)
    with core.store.transaction():
        core.store.execute("UPDATE outbox SET state='LEASED',lease_token=?,lease_until=?",
                           (token, after(core.clock(), 60)))
        module.validate_delivery(message_id, token)
    if mutation == "erase":
        with core.store.transaction():
            core.ledger.expire_events(core.store, [source], core.clock())
    elif mutation == "stop":
        stop(core, ref)
    elif mutation == "off":
        core.settings.life_loop = LifeLoopConfig()
    elif mutation == "quiet":
        core.settings.quiet_start, core.settings.quiet_end = 0, 23
    else:
        core.store.execute("UPDATE life_activity_results SET text='modified'")
    with core.store.transaction(), pytest.raises(CoreError):
        module.validate_delivery(message_id, token)
    assert core.store.one("SELECT dispatched_at FROM social_autonomy_reservations") == {
        "dispatched_at": None,
    }
