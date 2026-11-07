"""Cold-chat opportunities close on real context changes and on their original deadline.

The injected provider replies exercise scheduling and delivery, not model-quality accuracy.
"""

import pytest

from companion import notices, reply_revision, turn_taking
from companion.contracts import CoreError, LeaseRequest
from companion.input_debounce import InputDebounceConfig
from companion.transport import begin_send, enqueue, lease_deliveries
from tests.conftest import run
from tests.notice_owner_fixtures import run_notice
from tests.social_model_transport import NativeTestModel
from tests.support.input_debounce import accept as accept_batched
from tests.support.social_owner_authority import authorize
from tests.support.turn_taking import enable, lease, lively, system, turn

FOLLOWUP = "那个配角最后没有回头，我觉得比直接解释自己的选择更有意思。"


def queued(core, seconds=900):
    lively(core)
    core.clock.advance(seconds)
    assert turn_taking.flush(core.store, core.settings, core.clock())
    return core.store.one(
        (
            'SELECT j.*,e.body_json,e.conversation_id FROM jobs j JOIN events'
            " e USING(event_id) WHERE j.kind='activity' ORDER BY "
            'j.record_sequence DESC LIMIT 1'
        ),
    )


@pytest.mark.parametrize("closing", ["先聊到这", "我先走了", "困了，晚安"])
def test_explicit_closing_costs_no_followup_check(core, closing):
    # Chat-reply silence and proactive follow-up are independent switches.
    enable(core, silence_enabled=False)
    turn(core, 1, "今天把报告交了", "交完轻松多了。")
    core.clock.advance(60)
    turn(core, 2, closing, "好的。")
    core.clock.advance(900)

    assert not turn_taking.flush(core.store, core.settings, core.clock())
    assert len(core.model.calls) == 2
    assert not core.store.one("SELECT 1 FROM jobs WHERE kind='activity'")


@pytest.mark.parametrize("text", [
    "电影里他说晚安", "不要这么早说晚安", "我给它打了88", "“晚安”",
])
def test_mentions_and_quotations_are_not_treated_as_goodbye(core, text):
    enable(core)
    turn(core, 1, "我刚看完那部电影", "结尾的对白挺特别。")
    core.clock.advance(60)
    turn(core, 2, text, "这句台词和前面的场景很有联系。")
    core.clock.advance(900)
    core.model.activity.append(turn_taking.SKIP)

    assert turn_taking.flush(core.store, core.settings, core.clock())
    assert run(run_notice(core))
    assert len(core.model.calls) == 3
    assert not core.store.one("SELECT 1 FROM outbox WHERE action_type='qq.private.proactive'")
    # A legitimate contextual SKIP still consumes this opportunity exactly once.
    core.clock.advance(60)
    assert not turn_taking.flush(core.store, core.settings, core.clock())
    assert len(core.model.calls) == 3


@pytest.mark.parametrize(("previous", "owner", "answer", "eligible"), [
    ("交完轻松多了。", "嗯嗯", "好的", False),
    ("交完轻松多了。", "哈哈", "哈哈", False),
    ("报告交完了吗？", "嗯", "好的", True),
    ("交完轻松多了。", "嗯？", "好的", True),
    ("交完轻松多了。", "好的？", "好的", True),
    ("你给它打多少分？", "88", "好的", True),
    ("交完轻松多了。", "嗯", "那个图表让我想到了一个新的画法。", True),
    ("交完轻松多了。", "嗯", "那你准备怎么安排？", True),
    ("交完轻松多了。", "嗯", "好的？", True),
])
def test_acknowledgements_preserve_answers_and_new_content(
    core, previous, owner, answer, eligible,
):
    enable(core, silence_enabled=False)
    turn(core, 1, "今天把报告交了", previous)
    core.clock.advance(60)
    turn(core, 2, owner, answer)
    core.clock.advance(900)

    assert turn_taking.flush(core.store, core.settings, core.clock()) is eligible
    assert len(core.model.calls) == 2
    assert bool(core.store.one("SELECT 1 FROM jobs WHERE kind='activity'")) is eligible


@pytest.mark.parametrize("separate_bubbles", [False, True], ids=["same-bubble", "two-bubbles"])
def test_answer_to_an_earlier_question_is_not_a_closing(core, separate_bubbles):
    enable(core)
    first = turn(core, 1, "今天有点想喝茶", (
        "还要茶吗？" if separate_bubbles else "还要茶吗？我刚烧好水。"))
    if separate_bubbles:
        conversation = core.store.one(
            'SELECT conversation_id FROM events WHERE event_id=%s', (first["event_id"],),
        )["conversation_id"]
        tail = enqueue(core.store, first["event_id"], conversation, "我刚烧好水。",
                       core.clock(), key="question-followed-by-statement")
        core.store.execute(
            "UPDATE outbox SET state='SENT',sent_at=%s WHERE message_id=%s",
            (core.clock(), tail),
        )
    core.clock.advance(60)
    turn(core, 2, "嗯", "好的")

    assert turn_taking.SILENCE_RULES not in system(core)
    core.clock.advance(900)
    assert turn_taking.flush(core.store, core.settings, core.clock())


@pytest.mark.parametrize(("answer_delay", "batch_ack", "eligible"), [
    (60, False, True), (301, False, False), (60, True, True),
])
def test_batched_owner_messages_use_the_preceding_delivered_reply(
    core, answer_delay, batch_ack, eligible,
):
    enable(core)
    turn(core, 1, "我刚看完那部电影", "那个配角挺有意思。" if batch_ack else "结局你喜欢吗？")
    core.settings.input_debounce = InputDebounceConfig(enabled=True)
    core.clock.advance(answer_delay)
    # The legacy Core fixture needs the existing native-order debounce helper.
    # It runs real scheduling and reply supersession; no mocked jobs or delays.
    first, first_due = accept_batched(
        core, 2, "今天有点难过，想聊聊" if batch_ack else "还行，就是有点仓促")
    core.clock.advance(1)
    last, last_due = accept_batched(core, 3, "嗯" if batch_ack else "那个配角倒是挺有意思")
    assert first_due is not None and last_due > core.clock()
    assert not run(core.run_once("still-writing"))
    core.clock.advance(8)
    core.model.chat.append("好的" if batch_ack else "我也觉得，那个角色的选择比结局更耐琢磨。")
    assert run(core.run_once("answer-batch"))
    assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (first["job_id"],)) == {
        "state": "CANCELLED",
    }
    assert not core.store.one(
        'SELECT 1 FROM outbox WHERE origin_event_id=%s', (first["event_id"],),
    )
    core.store.execute(
        "UPDATE outbox SET state='SENT',sent_at=%s WHERE origin_event_id=%s AND state='PENDING'",
        (core.clock(), last["event_id"]),
    )
    core.clock.advance(900)

    assert turn_taking.flush(core.store, core.settings, core.clock()) is eligible
    assert len(core.model.calls) == 2
    if eligible:
        core.model.activity.append(FOLLOWUP)
        assert run(run_notice(core))
        assert [item["content"]["text"] for item in lease(core)] == [FOLLOWUP]
        assert len(core.model.calls) == 3


@pytest.mark.parametrize(("seconds", "eligible"), [
    (899, False), (900, True), (3600, True), (3601, False),
])
def test_material_and_scheduler_share_the_same_inclusive_time_window(core, seconds, eligible):
    enable(core)
    lively(core)
    ref = core.store.one(
        'SELECT message_id FROM outbox ORDER BY record_sequence DESC LIMIT 1'
    )["message_id"]
    core.clock.advance(seconds)

    assert (turn_taking.follow_up_target(core.store, core.settings, core.clock()) is not None) \
        is eligible
    if eligible:
        data, sources = turn_taking.follow_up_material(core.store, core.settings, ref, core.clock())
        assert data["kind"] == "followup" and sources[-1] == "evt_test_2"
    else:
        with pytest.raises(CoreError, match=reply_revision.SUPERSEDED):
            turn_taking.follow_up_material(core.store, core.settings, ref, core.clock())


def test_time_passing_inside_the_window_does_not_change_material_identity(core):
    enable(core)
    job = queued(core)
    original = notices.validate(core.store, core.settings, job, core.clock())
    core.clock.advance(60)

    assert notices.validate(core.store, core.settings, job, core.clock()) == original
    core.model.activity.append(FOLLOWUP)
    assert run(run_notice(core))
    assert [item["content"]["text"] for item in lease(core)] == [FOLLOWUP]


def test_expiry_while_queued_cancels_without_a_model_request(core):
    enable(core)
    job = queued(core)
    before = len(core.model.calls)
    core.clock.advance(2701)

    assert run(run_notice(core))
    assert core.store.one(
        'SELECT state,last_error_code FROM jobs WHERE job_id=%s', (job["job_id"],),
    ) == {"state": "CANCELLED", "last_error_code": reply_revision.SUPERSEDED}
    assert len(core.model.calls) == before
    assert not core.store.one("SELECT 1 FROM model_usage WHERE scenario='activity'")
    assert not turn_taking.flush(core.store, core.settings, core.clock())


def test_expiry_during_model_request_keeps_known_cost_and_discards_the_result(core, monkeypatch):
    enable(core)
    job = queued(core, seconds=3599)
    core.model.activity.append(FOLLOWUP)
    complete = core.model.complete

    async def delayed(messages, scenario):
        result = await complete(messages, scenario)
        core.clock.advance(2)
        return result

    monkeypatch.setattr(core.model, "complete", delayed)
    assert run(run_notice(core))

    usage = core.store.rows(
        "SELECT state,billed_micro_usd FROM model_usage WHERE scenario='activity'",
    )
    assert len(usage) == 1 and usage[0]["state"] == "SETTLED"
    assert usage[0]["billed_micro_usd"] > 0
    assert core.store.one(
        'SELECT state,last_error_code FROM jobs WHERE job_id=%s', (job["job_id"],),
    ) == {"state": "CANCELLED", "last_error_code": reply_revision.SUPERSEDED}
    assert not core.store.one("SELECT 1 FROM outbox WHERE action_type='qq.private.proactive'")
    assert not run(run_notice(core))
    assert len(core.model.calls) == 3


def test_expired_prepared_followup_does_not_block_the_next_owner_reply(core):
    enable(core)
    queued(core, seconds=3599)
    core.model.activity.append(FOLLOWUP)
    assert run(run_notice(core))
    core.clock.advance(2)

    assert lease(core) == []
    assert core.store.one(
        "SELECT state,last_error_code FROM outbox WHERE action_type='qq.private.proactive'",
    ) == {"state": "CANCELLED", "last_error_code": reply_revision.SUPERSEDED}
    turn(core, 3, "我回来了，接着聊那个配角吧", "我想到他最后那个表情。", deliver=False)
    assert [item["content"]["text"] for item in lease(core)] == ["我想到他最后那个表情。"]


@pytest.mark.parametrize("stage", ["lease", "send"])
def test_owner_authorized_delivery_rechecks_the_original_followup_deadline(
    core, tmp_path, stage,
):
    enable(core)
    lively(core)
    _, authority = authorize(core, tmp_path, account=False, additional_purposes=("proactive",))
    stub = core.model
    model = NativeTestModel(core.settings, stub, "activity")
    core.model = model
    try:
        core.clock.advance(3599)
        stub.activity.append(FOLLOWUP)
        assert turn_taking.flush(core.store, core.settings, core.clock())
        assert run(run_notice(core))
        usage = core.store.rows(
            "SELECT request_id,state,billed_micro_usd FROM model_usage WHERE scenario='activity'",
        )
        assert len(usage) == 1 and usage[0]["state"] == "SETTLED"
        request = LeaseRequest(worker_id="owner-bridge", authority=authority)
        if stage == "send":
            items = lease_deliveries(core.store, core.settings, request, core.clock())["items"]
            assert len(items) == 1
            item = items[0]
            core.clock.advance(2)
            with pytest.raises(CoreError, match=reply_revision.SUPERSEDED):
                begin_send(core.store, item["message_id"], item["lease_token"], core.clock(),
                           core.settings, clock=core.clock)
            assert not core.store.one("SELECT 1 FROM outbox WHERE state='SENDING'")
            # A rejected signed begin-send retains its lease until normal recovery.
            core.clock.advance(core.settings.lease_seconds)
        else:
            core.clock.advance(2)
        assert lease_deliveries(core.store, core.settings, request, core.clock())["items"] == []
        assert core.store.one(
            "SELECT state,last_error_code FROM outbox WHERE action_type='qq.private.proactive'",
        ) == {"state": "CANCELLED", "last_error_code": reply_revision.SUPERSEDED}
        assert not core.store.one(
            "SELECT 1 FROM outbox WHERE state IN ('PENDING','RETRY_WAIT','LEASED','SENDING')",
        )
        assert core.store.rows(
            "SELECT request_id,state,billed_micro_usd FROM model_usage WHERE scenario='activity'",
        ) == usage
        assert len(stub.calls) == 3
    finally:
        run(model.client.aclose())

