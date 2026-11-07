import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from companion.api import create_app
from companion.backup import backup, restore
from companion.contracts import CoreError, DeliveryResult, LeaseRequest
from companion.core import Core
from companion.goals import CommitmentRequest, GoalProposal, GoalScope
from companion.store import after
from companion.transport import begin_send, delivery_result, lease_deliveries, reconcile_delivery
from tests.conftest import ingest, run
from tests.test_observations import enable_observations, observation


def setup_goal(
    core, number=1, commitment=False, topic="了解天文内容", url="https://example.org/article"
):
    core.settings.goals_enabled = core.settings.exploration_enabled = True
    core.settings.wake_enabled = True
    enable_observations(core)
    ingest(core, number, "请阅读这篇天文文章")
    core.store.execute(
        "UPDATE jobs SET state='SUCCEEDED' WHERE event_id=%s", (f"evt_test_{number}",)
    )
    proposal = GoalProposal(
        topic=topic,
        source_event_ids=[f"evt_test_{number}"],
        scope=GoalScope(operation="read_content", url=url),
    )
    if commitment:
        return core.goals.accept_commitment(
            CommitmentRequest(
                proposal=proposal,
                accepted_event_id=f"evt_test_{number}",
                deadline=datetime.fromisoformat(after(core.clock(), 86400)),
            )
        )["goal_id"]
    return core.goals.propose(proposal)


def result_for(core, goal_id, number=1, **changes):
    text = f"实际获得的天文内容 {number}"
    item = observation(
        core,
        number,
        text=text,
        source_event_ids=core.goals.source_ids(goal_id),
        payload={"text": text, "url": json.loads(core.goals.get(goal_id)["scope_json"])["url"]},
        **changes,
    )
    core.observations.append("reader", item)
    return item.event_id


def test_goals_disabled_and_no_source_do_not_create_plans(core):
    proposal = GoalProposal(topic="无来源目标", source_event_ids=["missing"])
    with pytest.raises(CoreError, match="DISABLED"):
        core.goals.propose(proposal)
    core.settings.goals_enabled = True
    with pytest.raises(CoreError, match="SOURCE_UNAVAILABLE"):
        core.goals.propose(proposal)
    assert core.store.rows("SELECT * FROM goals") == []


def test_goals_idempotent_lease_and_real_related_result_required(core):
    goal = setup_goal(core)
    assert setup_goal(core) == goal
    core.goals.accept(goal)
    step = core.goals.claim()
    assert step and not core.goals.claim()
    with pytest.raises(CoreError, match="RESULT_NOT_SUCCESSFUL"):
        core.goals.finish(step, "nonexistent")
    unrelated = observation(core, 99)
    core.observations.append("reader", unrelated)
    with pytest.raises(CoreError, match="RESULT_NOT_RELATED"):
        core.goals.finish(step, unrelated.event_id)
    result = result_for(core, goal)
    assert core.goals.finish(step, result)["state"] == "DONE"
    with pytest.raises(CoreError, match="STALE_LEASE"):
        core.goals.finish(step, result)
    assert len(core.store.rows("SELECT * FROM traits WHERE category='interest'")) == 1
    assert core.store.rows("SELECT * FROM outbox") == []  # Self completion is not a share command.


def test_discovered_title_and_wrong_target_cannot_finish_goal(core):
    goal = setup_goal(core)
    core.goals.accept(goal)
    step = core.goals.claim()
    title = observation(
        core,
        1,
        text="视频标题",
        source_event_ids=core.goals.source_ids(goal),
        source_kind="title",
        result_status="DISCOVERED",
        payload={"title": "视频标题", "text": "视频标题"},
    )
    core.observations.append("reader", title)
    with pytest.raises(CoreError, match="NOT_SUCCESSFUL"):
        core.goals.finish(step, title.event_id)
    wrong = observation(
        core,
        2,
        source_event_ids=core.goals.source_ids(goal),
        payload={"text": "已获取的文章内容", "url": "https://example.org/other"},
    )
    core.observations.append("reader", wrong)
    with pytest.raises(CoreError, match="TARGET_MISMATCH"):
        core.goals.finish(step, wrong.event_id)


def test_goal_failure_retry_limit_and_restart_persistence(core):
    goal = setup_goal(core)
    core.goals.accept(goal)
    first = core.goals.claim()
    core.goals.fail(first, "FETCH_TIMEOUT", retryable=True)
    assert core.goals.get(goal)["state"] == "RETRY_WAIT"
    assert not core.goals.claim()
    core.clock.advance(86400)
    restarted = Core(core.settings, clock=core.clock)
    try:
        second = restarted.goals.claim()
        assert second["attempts"] == 2
        restarted.goals.fail(second, "FETCH_TIMEOUT", retryable=True)
        core.clock.advance(121)
        third = restarted.goals.claim()
        restarted.goals.fail(third, "FETCH_TIMEOUT", retryable=True)
        assert restarted.goals.get(goal)["state"] == "FAILED"
        assert not restarted.goals.claim()
    finally:
        restarted.store.close()


def test_resource_wait_does_not_consume_execution_retry_quota(core):
    goal = setup_goal(core)
    core.goals.accept(goal)
    first = core.goals.claim()
    core.goals.fail(first, "DAILY_QUOTA", resource=True)
    assert core.goals.get(goal)["state"] == "WAITING_RESOURCE"
    core.clock.advance(3601)
    assert core.goals.claim()["attempts"] == 1


def test_lost_goal_lease_rejects_old_worker_output(core):
    goal = setup_goal(core)
    core.goals.accept(goal)
    old = core.goals.claim()
    core.clock.advance(core.settings.lease_seconds + 1)
    new = core.goals.claim()
    result = result_for(core, goal)
    with pytest.raises(CoreError, match="STALE_LEASE"):
        core.goals.finish(old, result)
    assert core.goals.finish(new, result)["state"] == "DONE"


@pytest.mark.parametrize("success", [True, False])
def test_expiry_skips_goal_while_the_worker_finishes_its_step(core, monkeypatch, success):
    goal = setup_goal(core, commitment=True)
    step = core.goals.claim()
    result = result_for(core, goal)
    written, release = threading.Event(), threading.Event()
    execute = core.store.execute

    def pause_after_step(statement, parameters=()):
        count = execute(statement, parameters)
        if "UPDATE goal_steps SET state" in str(statement) and not written.is_set():
            written.set()
            assert release.wait(15)
        return count

    def finish():
        if success:
            return core.goals.finish(step, result)
        return core.goals.fail(step, "FETCH_TIMEOUT")

    # The second worker observes the deadline crossing during the first transaction.
    later = after(core.clock(), 86401)
    with Core(core.settings, core.model, lambda: later) as other, ThreadPoolExecutor(2) as pool:
        monkeypatch.setattr(core.store, "execute", pause_after_step)
        first = pool.submit(finish)
        try:
            assert written.wait(10)
            assert pool.submit(other.goals.claim).result(timeout=10) is None
        finally:
            release.set()
        assert first.result(timeout=10)
    assert core.goals.get(goal)["state"] == ("DONE" if success else "FAILED")


def test_commitment_ignores_temporary_affect_and_autonomy_pause(core):
    self_goal = setup_goal(core)
    core.goals.accept(self_goal)
    commitment = setup_goal(core, 2, commitment=True)
    core.settings.affect_enabled = True
    ingest(core, 3, "先不聊了")
    core.goals.pause_autonomy(True)
    step = core.goals.claim()
    assert step["goal_id"] == commitment
    result = result_for(core, commitment)
    core.goals.finish(step, result)
    record = core.store.one("SELECT * FROM commitments")
    assert record["state"] == "IN_PROGRESS" and record["feedback_outcome"] == "FULFILLED"
    assert (
        core.store.rows("SELECT * FROM traits") == []
    )  # Owner tasks do not become self interests.
    assert not core.goals.claim()


def test_commitment_feedback_uncertainty_requires_owner_reconciliation(core):
    goal = setup_goal(core, commitment=True)
    step = core.goals.claim()
    core.goals.finish(step, result_for(core, goal))
    job = core.store.one("SELECT job_id FROM jobs WHERE kind='activity' AND state='QUEUED'")
    assert job and run(core.run_once(job_id=job["job_id"]))
    first = lease_deliveries(
        core.store, core.settings, LeaseRequest(worker_id="bridge"), core.clock()
    )["items"][0]
    begin_send(core.store, first["message_id"], first["lease_token"], core.clock(), core.settings)
    delivery_result(core.store, core.settings, first["message_id"],
                    DeliveryResult(lease_token=first["lease_token"], result="SENT"), core.clock())
    delivery = lease_deliveries(
        core.store, core.settings, LeaseRequest(worker_id="bridge"), core.clock()
    )["items"][0]
    begin_send(core.store, delivery["message_id"], delivery["lease_token"],
               core.clock(), core.settings)
    delivery_result(
        core.store,
        core.settings,
        delivery["message_id"],
        DeliveryResult(lease_token=delivery["lease_token"], result="DELIVERY_UNCERTAIN"),
        core.clock(),
    )
    assert core.store.one("SELECT state FROM commitments")["state"] == "IN_PROGRESS"
    assert (
        lease_deliveries(core.store, core.settings, LeaseRequest(worker_id="bridge"), core.clock())[
            "items"
        ]
        == []
    )
    reconcile_delivery(core.store, delivery["message_id"], "SENT", core.clock())
    assert core.store.one("SELECT state FROM commitments")["state"] == "FULFILLED"


def test_commitment_deadline_queues_honest_failure_without_affect(core):
    goal = setup_goal(core, commitment=True)
    core.clock.advance(86401)
    assert not core.goals.claim()
    assert core.goals.get(goal)["state"] == "FAILED"
    record = core.store.one("SELECT * FROM commitments")
    assert record["state"] == "IN_PROGRESS" and record["feedback_outcome"] == "EXPLAINED_FAILURE"
    core.model.activity = "已经过了约定的时间，这次阅读没能完成。"
    assert not core.store.rows("SELECT * FROM outbox")
    job = core.store.one("SELECT job_id FROM jobs WHERE kind='activity' AND state='QUEUED'")
    assert job and run(core.run_once(job_id=job["job_id"]))
    body = json.loads(core.store.one("SELECT content_json FROM outbox")["content_json"])
    assert body["text"] == core.model.activity
    prompt = json.loads(core.model.calls[0][0][-1]["content"])
    assert prompt["failure_code"] == "DEADLINE_EXCEEDED"
    assert prompt["outcome"] == "EXPLAINED_FAILURE"


def test_goal_source_forget_and_restore_erase_scope_outcomes_and_interest(core, tmp_path):
    goal = setup_goal(core)
    core.goals.accept(goal)
    step = core.goals.claim()
    core.goals.finish(step, result_for(core, goal))
    key, archive, destination = (
        Fernet.generate_key(),
        tmp_path / "goals.enc",
        tmp_path / "restored.db",
    )
    backup(core.settings.database, archive, key)
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    restore(archive, destination, key, core.settings.erasure_ledger)
    restored = Core(
        core.settings.model_copy(update={"database": str(destination)}), clock=core.clock
    )
    try:
        for instance in (core, restored):
            assert instance.goals.recent() == []
            raw = instance.store.one("SELECT * FROM goals")
            assert raw["topic"] == "" and raw["scope_json"] == "{}"
            assert not instance.store.rows("SELECT * FROM traits")
            assert instance.observations.recent() == []
    finally:
        restored.store.close()


def test_wake_dedup_cooldown_and_daily_limit_survive_restart(core):
    first = setup_goal(core)
    second = setup_goal(core, 2)
    core.settings.wake_max_daily_runs = 1
    assert core.wake.evaluate()[0]["goal_id"] == first
    assert core.wake.evaluate() == []
    restarted = Core(core.settings, clock=core.clock)
    try:
        assert restarted.wake.evaluate() == []
        core.clock.advance(181 * 60)
        assert restarted.wake.evaluate() == []
        assert restarted.goals.get(second)["state"] == "CANDIDATE"
        assert (
            restarted.store.rows("SELECT reason_code FROM wake_decisions")[-1]["reason_code"]
            == "DAILY_LIMIT"
        )
    finally:
        restarted.store.close()


@pytest.mark.parametrize("reason", ["quiet", "pause", "unknown_price", "empty"])
def test_wake_can_skip_without_model_or_message(core, reason):
    setup_goal(core)
    if reason == "quiet":
        core.clock.now = datetime.fromisoformat("2026-10-19T16:00:00+00:00")
    elif reason == "pause":
        core.goals.pause_autonomy(True)
    elif reason == "unknown_price":
        core.settings.input_rate = None
    else:
        core.store.execute("UPDATE goals SET state='CANCELLED'")
    assert core.wake.evaluate() == []
    assert core.model.calls == []
    assert core.store.rows("SELECT * FROM outbox") == []


@pytest.mark.parametrize("diverse", [True, False])
def test_self_interest_requires_actual_diverse_reads_over_days(core, diverse):
    for day in range(7):
        goal = setup_goal(
            core,
            day + 1,
            topic="天文研究",
            url="https://example.org/article/" + str(day if diverse else 0),
        )
        core.goals.accept(goal)
        step = core.goals.claim()
        core.goals.finish(step, result_for(core, goal, day + 1))
        current = core.store.one("SELECT * FROM traits WHERE category='interest'")
        assert (
            current["status"] != "STABLE"
            if day < 6 or not diverse
            else current["status"] == "STABLE"
        )
        core.clock.advance(86400)
    assert current["independent_days"] == (7 if diverse else 1)
    assert core.store.rows("SELECT * FROM memories WHERE subject='user'") == []


def test_owner_control_api_and_qq_commands_are_independent_from_model(core):
    setup_goal(core)
    with TestClient(create_app(core.settings)) as client:
        bridge = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
        admin = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
        assert client.get("/internal/v2/goals", headers=bridge).status_code == 401
        assert client.get("/internal/v2/goals", headers=admin).status_code == 200
    ingest(core, 100, "/安排任务 https://example.org/article 阅读天文介绍")
    assert run(core.run_once())
    assert len(core.store.rows("SELECT * FROM commitments")) == 1
    assert core.model.calls == []
    event = core.store.one("SELECT * FROM events WHERE event_id='evt_test_100'")
    assert event["memory_processed"] and json.loads(event["body_json"])["control_command"]


def test_restored_pending_commitment_feedback_is_never_blindly_replayed(core, tmp_path):
    goal = setup_goal(core, commitment=True)
    step = core.goals.claim()
    core.goals.finish(step, result_for(core, goal))
    job = core.store.one("SELECT job_id FROM jobs WHERE kind='activity' AND state='QUEUED'")
    assert job and run(core.run_once(job_id=job["job_id"]))
    key, archive, destination = (
        Fernet.generate_key(),
        tmp_path / "delivery.enc",
        tmp_path / "restored.db",
    )
    backup(core.settings.database, archive, key)
    restore(archive, destination, key, core.settings.erasure_ledger)
    restored = Core(
        core.settings.model_copy(update={"database": str(destination)}), clock=core.clock
    )
    try:
        assert (
            lease_deliveries(
                restored.store, restored.settings, LeaseRequest(worker_id="bridge"), core.clock()
            )["items"]
            == []
        )
        assert restored.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"
        assert restored.store.one("SELECT state FROM commitments")["state"] == "IN_PROGRESS"
    finally:
        restored.store.close()
