"""Source-bound Life wording reaches native channel custody without a second model."""

import json

import pytest

from companion import paid_budget, social_autonomy_runtime
from companion.contracts import CoreError
from companion.life_protocol import (
    LifeLoopConfig,
)
from companion.reply_segments import SegmentedReplyConfig
from companion.social import proactive_counts
from companion.social_autonomy import SocialAutonomy
from companion.store import after, encode, uid
from tests.conftest import run
from tests.support.life_expression_consumption import (
    close_provider_clients as close_provider_clients,
)
from tests.support.life_expression_consumption import (
    prepared,
    request,
)
from tests.support.owner_authority import activate
from tests.support.social_owner_authority import stop


def test_diary_save_refuses_an_ordinary_transaction_before_writing(core, tmp_path):
    core.settings.diary_enabled = True
    job, _, authority, _ = prepared(core, tmp_path, destination="PRIVATE_DIARY", purposes=["diary"])
    body = request(core, job, authority, "diary.save")
    with core.store.transaction():
        writes = core.store.change_token()["writes"]
        with pytest.raises(RuntimeError, match="ERASURE_CONTROL_WRITE_REQUIRED"):
            core.life_topics.consume(job, body, diary=core.diary)
        assert core.store.change_token()["writes"] == writes
    assert not core.store.rows("SELECT * FROM diary_entries")


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
    core.store.execute('UPDATE outbox SET content_json=%s,content_sha256=%s WHERE message_id=%s',
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
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, [source], core.clock())
    elif mutation == "fiction":
        core.store.execute("UPDATE life_activity_results SET proposal_json="
                           "jsonb_set(proposal_json::jsonb,'{fiction}','true')::text")
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
        core.store.execute("UPDATE outbox SET state='LEASED',lease_token=%s,lease_until=%s",
                           (token, after(core.clock(), 60)))
        module.validate_delivery(message_id, token)
    if mutation == "erase":
        with core.store.transaction(control_write=True):
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


def test_expired_private_life_draft_retires_instead_of_failing_every_lease(core, tmp_path):
    from datetime import datetime

    from companion.contracts import LeaseRequest
    from companion.transport import lease_deliveries

    job_id, _, ref, _ = prepared(core, tmp_path, destination="PRIVATE_MESSAGE_DRAFT")
    core.life_topics.consume(job_id, request(core, job_id, ref, "qq.private.proactive"))
    usage = core.store.rows("SELECT * FROM model_usage")
    expires = core.store.one(
        (
            'SELECT t.expires_at FROM life_topics t JOIN '
            'life_activity_results r USING(topic_id) WHERE r.job_id=%s'
        ), (job_id,))["expires_at"]
    core.clock.advance((datetime.fromisoformat(expires)
                        - datetime.fromisoformat(core.clock())).total_seconds() + 1)
    lease = LeaseRequest(worker_id="owner-bridge", authority=ref)

    assert lease_deliveries(core.store, core.settings, lease, core.clock())["items"] == []
    assert core.store.rows("SELECT state,last_error_code FROM outbox") == [
        {"state": "CANCELLED", "last_error_code": "LIFE_DRAFT_EXPIRED"}]
    assert lease_deliveries(core.store, core.settings, lease, core.clock())["items"] == []
    # Known model cost and its audit stay; only the stale delivery leaves the queue.
    assert core.store.rows("SELECT * FROM model_usage") == usage
