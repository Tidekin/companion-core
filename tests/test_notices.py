"""Generated automatic messages retain source, budget and ordered delivery custody."""

import hashlib
import json

import pytest

from companion import budget
from companion.contracts import DeliveryResult, LeaseRequest
from companion.reply_segments import SegmentedReplyConfig
from companion.social import proactive_counts
from companion.transport import begin_send, delivery_result, lease_deliveries
from tests.conftest import run
from tests.notice_owner_fixtures import run_notice
from tests.support.goals import result_for, setup_goal
from tests.support.notices import NoticeModel, ready_share
from tests.support.source_definitions import BILI, accepted_text, enable_sharing


@pytest.mark.parametrize(
    "wording", ["这个话题让我想到我们之前的聊天。", "这段内容挺有意思，想听听你的想法。"],
)
def test_share_uses_generated_wording_then_a_link_only_bubble(core, wording):
    event = ready_share(core, wording)
    assert not core.store.rows("SELECT * FROM outbox")
    assert not core.social.flush()
    assert run(run_notice(core))
    rows = core.store.rows('SELECT * FROM outbox ORDER BY record_sequence')
    assert [json.loads(row["content_json"])["text"] for row in rows] == [wording, BILI]
    assert rows[0]["reply_group_id"] == rows[1]["reply_group_id"]
    assert len(core.model.calls) == 1
    prompt = json.loads(core.model.calls[0][0][-1]["content"])
    assert prompt["text"] == event.payload.text and prompt["title"] == event.payload.title
    assert proactive_counts(core.store, core.settings, core.clock())[0] == 1
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    assert json.loads(core.store.one('SELECT body_json FROM events WHERE event_id=%s',
                                    (event.event_id,))["body_json"])["text"] == event.payload.text
    first = lease_deliveries(core.store, core.settings, LeaseRequest(worker_id="bridge"),
                             core.clock())["items"]
    assert len(first) == 1 and first[0]["content"] == {"text": wording}
    assert not lease_deliveries(core.store, core.settings, LeaseRequest(worker_id="other"),
                                core.clock())["items"]
    begin_send(core.store, first[0]["message_id"], first[0]["lease_token"],
               core.clock(), core.settings)
    delivery_result(core.store, core.settings, first[0]["message_id"],
                    DeliveryResult(lease_token=first[0]["lease_token"], result="SENT"),
                    core.clock())
    core.clock.advance(1)
    second = lease_deliveries(core.store, core.settings, LeaseRequest(worker_id="bridge"),
                              core.clock())["items"]
    assert len(second) == 1 and second[0]["content"] == {"text": BILI}


@pytest.mark.parametrize("limit,expected", [("unlimited", 4), (3, 3), (2, 2), (None, 2)])
def test_share_segments_prose_with_a_reserved_link_slot(core, limit, expected):
    core.settings.segmented_reply = (SegmentedReplyConfig(mode="semantic", max_segments=limit)
                                     if limit is not None else None)
    wording = "看到了一个有趣的细节。\n\n镜头转向窗外。\n\n这个安排让我想起另一部片子。"
    ready_share(core, wording)
    assert run(run_notice(core))
    rows = core.store.rows('SELECT * FROM outbox ORDER BY record_sequence')
    texts = [json.loads(row["content_json"])["text"] for row in rows]
    assert len(texts) == expected and texts[-1] == BILI
    assert "".join(texts[:-1]) == wording
    assert len(core.model.calls) == 1
    assert proactive_counts(core.store, core.settings, core.clock())[0] == 1


def test_share_skip_keeps_the_real_model_charge_without_an_outbox(core):
    ready_share(core, "[SKIP]")
    assert run(run_notice(core))
    assert core.store.one("SELECT state FROM social_candidates")["state"] == "SKIPPED"
    assert proactive_counts(core.store, core.settings, core.clock()) == (0, False)
    assert not core.store.rows("SELECT * FROM outbox")
    assert len(core.model.calls) == 1
    assert core.store.one("SELECT billed_micro_usd FROM model_usage")["billed_micro_usd"] > 0


def test_unreserved_slot_refusal_waits_for_the_same_wording_job(core, monkeypatch):
    ready_share(core, "这个话题值得一聊。")
    reserve = budget.reserve

    def occupied(*args, **kwargs):
        from companion.contracts import CoreError

        raise CoreError("MODEL_PROVIDER_CONCURRENCY_EXHAUSTED", 429)

    monkeypatch.setattr(budget, "reserve", occupied)
    assert run(run_notice(core))
    row = core.store.one("SELECT * FROM jobs WHERE kind='activity'")
    assert row["state"] == "RETRY_WAIT" and row["attempts"] == 0
    assert not core.model.calls and not core.store.rows("SELECT * FROM outbox")
    core.clock.advance(61)
    monkeypatch.setattr(budget, "reserve", reserve)
    assert run(run_notice(core))
    assert len(core.model.calls) == 1


def test_accepted_task_feedback_uses_the_model_and_waits_for_both_acks(core):
    goal = setup_goal(core, commitment=True)
    step = core.goals.claim()
    core.goals.finish(step, result_for(core, goal))
    core.goals.pause_autonomy(True)
    wording = "天文资料读到了，里面提到的现象很有意思。"
    core.model = NoticeModel(wording)
    assert not core.store.rows("SELECT * FROM outbox")
    assert run(run_notice(core))
    assert proactive_counts(core.store, core.settings, core.clock())[0] == 0
    for index in range(2):
        item = lease_deliveries(core.store, core.settings, LeaseRequest(worker_id="bridge"),
                                core.clock())["items"][0]
        assert item["content"] == {"text": wording if index == 0 else "https://example.org/article"}
        assert core.store.one("SELECT state FROM commitments")["state"] == "IN_PROGRESS"
        begin_send(core.store, item["message_id"], item["lease_token"], core.clock(), core.settings)
        delivery_result(core.store, core.settings, item["message_id"],
                        DeliveryResult(lease_token=item["lease_token"], result="SENT"),
                        core.clock())
    assert core.store.one("SELECT state FROM commitments")["state"] == "FULFILLED"
    prompt = json.loads(core.model.calls[0][0][-1]["content"])
    assert prompt["outcome"] == "FULFILLED" and "实际获得的天文内容" in prompt["text"]


def test_failed_task_wording_receives_the_real_failure_and_cannot_skip(core):
    goal = setup_goal(core, commitment=True)
    core.clock.advance(86401)
    assert not core.goals.claim()
    core.model = NoticeModel("[SKIP]")
    assert run(run_notice(core))
    assert not core.store.rows("SELECT * FROM outbox")
    assert core.store.one("SELECT state FROM commitments")["state"] == "IN_PROGRESS"
    assert core.goals.get(goal)["state"] == "FAILED"
    prompt = json.loads(core.model.calls[0][0][-1]["content"])
    assert prompt["outcome"] == "EXPLAINED_FAILURE"
    assert prompt["failure_code"] == "DEADLINE_EXCEEDED"


def test_first_bubble_uncertainty_prevents_the_link_from_being_sent(core):
    ready_share(core, "刚读到这段资料，想与你分享。")
    assert run(run_notice(core))
    item = lease_deliveries(core.store, core.settings, LeaseRequest(worker_id="bridge"),
                            core.clock())["items"][0]
    begin_send(core.store, item["message_id"], item["lease_token"], core.clock(), core.settings)
    delivery_result(core.store, core.settings, item["message_id"],
                    DeliveryResult(lease_token=item["lease_token"], result="DELIVERY_UNCERTAIN"),
                    core.clock())
    assert not lease_deliveries(core.store, core.settings, LeaseRequest(worker_id="bridge"),
                                core.clock())["items"]
    assert [row["state"] for row in core.store.rows(
        'SELECT state FROM outbox ORDER BY record_sequence'
    )] == [
        "DELIVERY_UNCERTAIN", "CANCELLED",
    ]


def test_long_share_prompt_marks_excerpt_and_respects_context_capacity(core):
    enable_sharing(core)
    event = accepted_text(core)
    event.payload.text = "这是一段真实读取的长资料。" * 500
    event.source_sha256 = hashlib.sha256(event.payload.text.encode()).hexdigest()
    core.observations.append("reader", event)
    core.social.consider(event.event_id)
    core.settings.context_soft_tokens = 6000
    core.model = NoticeModel("其中的这个话题很有意思。")
    assert core.social.flush() and run(run_notice(core))
    messages, _ = core.model.calls[0]
    payload = json.loads(messages[-1]["content"])
    assert payload["excerpt_truncated"] is True and payload["text"]
    assert event.payload.text.startswith(payload["text"])
    assert budget.token_upper_bound(messages) <= core.settings.context_soft_tokens
