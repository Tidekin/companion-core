"""Supersession preserves sent evidence and accounting while fencing old work."""

import json

import pytest

from companion import budget, dialogue_history, private_life, reply_revision
from companion.contracts import CoreError, DeliveryResult
from companion.core import Core
from companion.reply_segments import SegmentedReplyConfig
from companion.transport import (
    begin_send,
    delivery_result,
    enqueue,
    enqueue_reply_parts,
)
from tests.conftest import ingest, run
from tests.test_segmented_delivery import PARTS, acknowledge, lease


def accept(core, number=1, text="一起聊接下来的计划", **kwargs):
    """Exercise the durable transition without enabling the production live guard."""
    result = ingest(core, number, text, **kwargs)
    if not result["duplicate"]:
        event = core.store.one("SELECT * FROM events WHERE event_id=?", (result["event_id"],))
        with core.store.transaction():
            result["reply_revision"] = reply_revision.advance(
                core.store, event["conversation_id"], event["event_id"], core.clock(),
            )
    return result


def frozen(core, parts=PARTS):
    first = accept(core)
    job = core.claim_job("first-worker")
    ids = enqueue_reply_parts(core.store, job, parts, 1500, core.clock())
    core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?", (first["job_id"],))
    return first, job, ids


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING", "RETRY_WAIT"])
def test_new_input_cancels_only_same_conversation_pending_job(core, state):
    first = accept(core)
    core.store.execute("UPDATE jobs SET state=? WHERE job_id=?", (state, first["job_id"]))
    unrelated = accept(core, 7, origin=f"other:FriendMessage:{core.settings.owner_qq_id}")
    second = accept(core, 2, "请先听完我的补充")
    assert second["reply_revision"] == 2
    assert core.store.one("SELECT state,last_error_code FROM jobs WHERE job_id=?",
                          (first["job_id"],)) == {
        "state": "CANCELLED", "last_error_code": reply_revision.SUPERSEDED,
    }
    assert core.store.one("SELECT state FROM jobs WHERE job_id=?", (unrelated["job_id"],)) == {
        "state": "QUEUED",
    }
    assert core.store.one("SELECT redacted FROM events WHERE event_id=?", (first["event_id"],)) == {
        "redacted": 0,
    }
    duplicate = accept(core, 2, "请先听完我的补充")
    assert duplicate["duplicate"] and duplicate["reply_revision"] == 2
    assert core.store.one("SELECT state FROM jobs WHERE job_id=?", (second["job_id"],)) == {
        "state": "QUEUED",
    }


@pytest.mark.parametrize("kind", ["single", "segmented", "image_intent"])
def test_late_model_completion_settles_but_cannot_publish_old_reply(core, kind):
    if kind == "segmented":
        core.settings.segmented_reply = SegmentedReplyConfig(min_chars=4)
    if kind == "image_intent":
        core.settings.expression_enabled = True
        core.settings.expression_meme_categories = ["happy"]
    first = accept(core, text="".join(PARTS) + ("⬡happy⬡" if kind == "image_intent" else ""))
    core.model.callback = lambda: accept(core, 2, "等一下，还有补充")
    assert run(core.run_once())
    assert core.store.rows("SELECT * FROM outbox") == []
    assert core.store.one("SELECT state FROM jobs WHERE job_id=?", (first["job_id"],)) == {
        "state": "CANCELLED",
    }
    assert core.store.one("SELECT state,billed_micro_usd FROM model_usage") == {
        "state": "SETTLED", "billed_micro_usd": budget.cost(core.settings, 100, 20, 40),
    }
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key LIKE 'live_text_stop%'")


@pytest.mark.parametrize("parts", [["一条完整回复"], PARTS])
def test_leased_single_and_segmented_reply_cannot_begin_after_new_input(core, parts):
    _, _, ids = frozen(core, parts)
    pending = lease(core)[0]
    accept(core, 2, "先等一等")
    with pytest.raises(CoreError, match="STALE_DELIVERY_LEASE"):
        begin_send(core.store, pending["message_id"], pending["lease_token"], core.clock(),
                   core.settings)
    assert core.store.one("SELECT lease_token FROM outbox WHERE message_id=?",
                          (pending["message_id"],)) == {"lease_token": pending["lease_token"]}
    assert [row["state"] for row in core.store.rows("SELECT state FROM outbox ORDER BY rowid")] == [
        "CANCELLED" for _ in ids
    ]
    assert lease(core) == []


@pytest.mark.parametrize("result", ["SENT", "DELIVERY_UNCERTAIN"])
def test_inflight_result_survives_new_input_without_replaying_its_tail(core, result):
    _, _, ids = frozen(core)
    item = lease(core)[0]
    begin_send(core.store, item["message_id"], item["lease_token"], core.clock(), core.settings)
    accept(core, 2, "我的话还没说完")
    assert core.store.one("SELECT state FROM outbox WHERE message_id=?", (ids[0],)) == {
        "state": "SENDING",
    }
    delivery_result(core.store, core.settings, ids[0], DeliveryResult(
        lease_token=item["lease_token"], result=result,
        provider_message_id="123" if result == "SENT" else None,
    ), core.clock())
    assert [row["state"] for row in core.store.rows("SELECT state FROM outbox ORDER BY rowid")] == [
        result, "CANCELLED", "CANCELLED",
    ]
    assert lease(core) == []


def test_companion_worker_waits_for_send_ack_after_accepting_a_new_input(core, monkeypatch):
    _, _, ids = frozen(core)
    item = lease(core)[0]
    begin_send(core.store, item["message_id"], item["lease_token"], core.clock(), core.settings)
    second = accept(core, 2, "先听我补充")
    monkeypatch.setattr(private_life, "enabled", lambda settings: True)
    assert core.claim_job("next-worker") is None
    assert core.store.one("SELECT state,attempts FROM jobs WHERE job_id=?",
                          (second["job_id"],)) == {
        "state": "QUEUED", "attempts": 0,
    }
    delivery_result(core.store, core.settings, ids[0], DeliveryResult(
        lease_token=item["lease_token"], result="SENT", provider_message_id="123",
    ), core.clock())
    assert core.claim_job("next-worker")["job_id"] == second["job_id"]


def test_history_contains_only_delivered_prefix_and_pending_inputs_in_order(core):
    _, job, ids = frozen(core)
    acknowledge(core, lease(core)[0])
    accept(core, 2, "第二条补充")
    accept(core, 3, "第三条问题")
    history = dialogue_history.recent_context_sources(
        core.store, job["conversation_id"], "evt_test_3", 10,
    )
    assert [row["event_id"] for row in history] == ["evt_test_1", "evt_test_2"]
    assert [row["reply_text"] for row in history] == [PARTS[0], ""]
    assert history[0]["reply_segments"][0]["message_id"] == ids[0]
    assert len(history[0]["reply_segments"]) == 1
    assert all(row["reply_complete"] is False and "reply_revision" not in row for row in history)
    roles = [message["role"] for row in history for message in dialogue_history.turn_messages(row)]
    assert roles == [
        "user", "assistant", "user",
    ]
    assert not dialogue_history.recent_sent_sources(
        core.store, job["conversation_id"], "evt_test_3", 10,
    )


def test_partial_history_still_checks_the_entire_frozen_manifest(core):
    _, job, ids = frozen(core)
    acknowledge(core, lease(core)[0])
    accept(core, 2)
    core.store.execute("UPDATE outbox SET content_json=? WHERE message_id=?",
                       (json.dumps({"text": "altered unsent text"}), ids[2]))
    assert dialogue_history.recent_context_sources(
        core.store, job["conversation_id"], "evt_test_2", 10,
    ) == []


def test_restart_cannot_requeue_superseded_work_or_refund_usage(core):
    first = accept(core)
    job = core.claim_job("first-worker")
    request = budget.reserve(core.store, core.settings, job["job_id"], "chat",
                             [{"role": "user", "content": "first"}], core.clock())
    before = core.store.one("SELECT * FROM model_usage WHERE request_id=?", (request,))
    accept(core, 2)
    core.clock.advance(core.settings.lease_seconds + 1)
    with Core(core.settings, core.model, core.clock) as restarted:
        claimed = restarted.claim_job("restarted-worker")
        assert claimed["event_id"] == "evt_test_2"
        assert restarted.store.one("SELECT state FROM jobs WHERE job_id=?", (first["job_id"],)) == {
            "state": "CANCELLED",
        }
        assert restarted.store.one("SELECT * FROM model_usage WHERE request_id=?",
                                   (request,)) == before
        with pytest.raises(CoreError, match="REPLY_SUPERSEDED"):
            enqueue(restarted.store, first["event_id"], claimed["conversation_id"],
                    "stale", core.clock())


def test_source_tracking_fences_a_reply_without_linking_independent_owner_inputs(core):
    accept(core)
    second = accept(core, 2)
    job = core.claim_job("latest-worker")
    reply_revision.remember_sources(core.store, job, ["evt_test_1"])
    assert core.store.rows("SELECT event_id FROM job_sources WHERE job_id=? ORDER BY event_id",
                           (second["job_id"],)) == [
        {"event_id": "evt_test_1"}, {"event_id": "evt_test_2"},
    ]
    assert core.store.rows("SELECT * FROM event_dependencies") == []
    core.store.execute("UPDATE events SET redacted=1,body_json='{}' WHERE event_id='evt_test_1'")
    with pytest.raises(CoreError, match="REPLY_SOURCE_RECALLED"):
        reply_revision.check_job(core.store, job)
    assert core.store.one("SELECT redacted FROM events WHERE event_id='evt_test_2'") == {
        "redacted": 0,
    }
    core.fail_job(job, reply_revision.SOURCE_RECALLED)
    assert core.store.one("SELECT state FROM jobs WHERE job_id=?", (second["job_id"],)) == {
        "state": "CANCELLED",
    }
    assert core.store.rows("SELECT * FROM outbox") == []


def test_unadmitted_provider_slot_race_waits_without_spending_retry_or_stopping(core, monkeypatch):
    first = accept(core)
    job = core.claim_job("racing-worker")
    monkeypatch.setattr(private_life, "enabled", lambda settings: True)
    core.fail_job(job, "MODEL_PROVIDER_CONCURRENCY_EXHAUSTED")
    assert core.store.one("SELECT state,attempts FROM jobs WHERE job_id=?", (first["job_id"],)) == {
        "state": "RETRY_WAIT", "attempts": 0,
    }
    assert core.store.rows("SELECT * FROM model_usage") == []
    assert core.store.rows("SELECT * FROM outbox") == []
    assert not core.store.one("SELECT 1 FROM runtime_meta WHERE key LIKE 'live_text_stop%'")


@pytest.mark.parametrize("keep_history", [False, True])
def test_prompt_lineage_tracks_only_history_retained_after_context_bounds(core, keep_history):
    ingest(core, text="已送达的旧问题")
    assert run(core.run_once())
    acknowledge(core, lease(core)[0])
    second = ingest(core, 2, "这次独立的问题")
    job = core.claim_job("context-worker")
    core.settings.max_recent_messages = 4
    core.settings.context_soft_tokens = 16000 if keep_history else 1
    prompt = core.context(job)
    assert ("已送达的旧问题" in json.dumps(prompt, ensure_ascii=False)) is keep_history
    assert {row["event_id"] for row in core.store.rows(
        "SELECT event_id FROM job_sources WHERE job_id=?", (second["job_id"],),
    )} == ({"evt_test_1", "evt_test_2"} if keep_history else {"evt_test_2"})


def test_source_opaque_read_command_is_explicitly_conservative(core):
    first = ingest(core, text="/日记列表")
    assert run(core.run_once())
    assert core.store.one("SELECT context_tracked FROM jobs WHERE job_id=?",
                          (first["job_id"],)) == {
        "context_tracked": 0,
    }


def test_owner_interrupts_dispatched_activity_without_poisoning_observed_usage(
    core, tmp_path, monkeypatch,
):
    from tests.test_life_worker_integration import (
        test_real_http_source_to_independent_worker_shadow as produce,
    )

    produce(core, tmp_path, monkeypatch, "preempt")
    result = core.store.one("SELECT * FROM life_activity_results")
    assert core.store.one("SELECT state FROM jobs WHERE job_id=?", (result["job_id"],)) == {
        "state": "CANCELLED"}
    usage = core.store.one("SELECT * FROM model_usage WHERE job_id=?", (result["job_id"],))
    assert usage["state"] == "SETTLED" and usage["billed_micro_usd"] > 0
    assert not core.store.one("SELECT 1 FROM outbox WHERE action_type='qq.private.proactive'")


def test_a_failed_reply_keeps_its_owner_input_in_history_without_inventing_a_reply(core):
    # Production 2026-10-30: a provider error failed one chat job; the next reply never
    # saw that question because only superseded inputs were kept as unanswered.
    first = accept(core, 1, "我什么时候说过凌晨三点")
    core.store.execute("UPDATE jobs SET state='FAILED',last_error_code='MODEL_RETRYABLE' "
                       "WHERE job_id=?", (first["job_id"],))
    accept(core, 2, "？在吗")
    conversation = core.store.one("SELECT conversation_id FROM events WHERE event_id=?",
                                  (first["event_id"],))["conversation_id"]
    history = dialogue_history.recent_context_sources(core.store, conversation, "evt_test_2", 10)
    assert [row["event_id"] for row in history] == ["evt_test_1"]
    assert history[0]["reply_text"] == "" and history[0]["reply_complete"] is False
    roles = [message["role"] for message in dialogue_history.turn_messages(history[0])]
    assert roles == ["user"]


def test_newer_input_stops_an_old_context_build_midway(core, monkeypatch):
    first = accept(core)
    job = core.claim_job("first-worker")
    assert job["job_id"] == first["job_id"]
    history = []
    original = dialogue_history.turn_messages

    def turn_messages(source, store):
        history.append(source)
        return original(source, store)

    def newer_input(*_args, **_kwargs):
        accept(core, 2, "请先听完我的补充")
        return None

    # The newer message lands while the old job is still collecting references.
    monkeypatch.setattr(core, "_expression_learning_reference", newer_input)
    monkeypatch.setattr(dialogue_history, "turn_messages", turn_messages)
    with pytest.raises(CoreError) as error:
        core.context(job)
    assert error.value.code in {reply_revision.CANCELLED, reply_revision.SUPERSEDED}
    assert history == []  # History and planning never ran for the cancelled job.
