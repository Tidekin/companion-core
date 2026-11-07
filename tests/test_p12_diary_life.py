"""Native services with synthetic sources/clocks; no real-day or phone evidence."""

from datetime import date, datetime

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from companion.api import create_app
from companion.backup import backup, restore
from companion.contracts import CoreError, DeliveryResult, LeaseRequest
from companion.core import Core
from companion.goals import GoalProposal, GoalScope
from companion.privacy import epoch
from companion.store import encode
from companion.transport import begin_send, delivery_result, enqueue, lease_deliveries
from tests.conftest import ingest, run
from tests.support.exploration import configure
from tests.support.source_grants import prepare
from tests.test_consolidation import enable_nightly
from tests.test_diary import DAY
from tests.test_goals import result_for, setup_goal
from tests.test_heybox_reader import URL
from tests.test_observations import observation


def test_native_daily_pipeline_revision_next_day_and_latest_ledger_restore(core, tmp_path):
    model = enable_nightly(core)
    core.consolidation.schedule_day(DAY)
    assert run(core.consolidation.run_once())
    assert core.consolidation.status(DAY)["state"] == "COMPLETE"
    assert [scenario for _, scenario in model.calls] == ["diary", "reflection"]
    original = core.diary.get_day(DAY, core.settings.owner_qq_id)
    assert original and "主人说" in original["body_md"]
    prior_epoch = epoch(core.store)
    corrected = core.diary.correct(DAY, "主人核对后的私人修订", core.settings.owner_qq_id)
    assert "主人修订" in corrected["body_md"] and epoch(core.store) > prior_epoch
    assert len(core.store.rows("SELECT * FROM diary_revisions")) == 1

    # Continuity uses the original received events, never the diary's assertion.
    prior_events = core.store.rows("SELECT * FROM events WHERE source='qq.onebot'")
    core.memory.episodes(prior_events, core.clock())
    core.settings.memory_enabled = True
    core.clock.advance(86400)
    next_day = date(2026, 10, 19)
    for number in range(10, 13):
        ingest(core, number, f"继续聊读书计划 {number}", at="2026-10-19T04:00:00Z")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    sources = core.diary.repo.sources(next_day)
    assert any(source["kind"] == "prior_owner_message" for source in sources)
    assert "主人核对后的私人修订" not in encode(sources)
    core.diary.schedule_day(next_day)
    assert run(core.diary.generate_once("p12-diary-fixture"))
    assert core.diary.get_day(next_day, core.settings.owner_qq_id)
    assert not core.store.rows("SELECT * FROM outbox")

    key, archive, dest = Fernet.generate_key(), tmp_path / "life.enc", tmp_path / "restore.db"
    backup(core.settings.database, archive, key)
    core.diary.forget(DAY, core.settings.owner_qq_id)
    restore(archive, dest, key, core.settings.erasure_ledger)
    restored = Core(core.settings.model_copy(update={"database": str(dest)}), clock=core.clock)
    try:
        for instance in (core, restored):
            assert instance.diary.get_day(DAY, core.settings.owner_qq_id) is None
            assert instance.diary.schedule_day(DAY) is None
            assert instance.diary.get_day(next_day, core.settings.owner_qq_id)
            assert instance.store.rows("SELECT * FROM diary_revisions") == []
            assert not instance.store.rows("SELECT * FROM outbox")
        assert core.diary.status(core.settings.owner_qq_id)["auto_share"] is False
    finally:
        restored.store.close()


def test_goal_shadow_pause_failure_and_restart_keep_intention_separate_from_execution(core):
    goal_id = setup_goal(core)
    before = core.store.db.total_changes
    shadow = core.goals.recent()[0]
    assert shadow["mode"] == "SHADOW" and shadow["state"] == "CANDIDATE"
    assert shadow["source_evidence"][0]["event_id"] == "evt_test_1"
    assert shadow["execution"] is shadow["result"] is shadow["delivery"] is None
    assert shadow["phone_confirmation"] is None and core.store.db.total_changes == before
    core.goals.accept(goal_id)
    first = core.goals.claim()
    core.goals.pause_autonomy(True)
    assert core.goals.recent()[0]["execution"]["state"] == "WAITING_RESOURCE"
    with pytest.raises(CoreError, match="STALE_LEASE"):
        core.goals.finish(first, result_for(core, goal_id))
    restarted = Core(core.settings, core.model, core.clock)
    try:
        assert restarted.goals.claim() is None
        restarted.goals.pause_autonomy(False)
        # Pausing a running step retains the native resource retry deadline.
        assert restarted.goals.claim() is None
        core.clock.now = datetime.fromisoformat(restarted.goals.get(goal_id)["next_due_at"])
        second = restarted.goals.claim()
        assert second is not None
        restarted.goals.fail(second, "FETCH_UNKNOWN", retryable=False)
        result = restarted.goals.recent()[0]
        assert result["state"] == "FAILED" and result["result"] is None
        assert result["execution"]["last_error_code"] == "FETCH_UNKNOWN"
        assert result["delivery"] is result["phone_confirmation"] is None
        assert not restarted.store.rows("SELECT * FROM outbox") and not core.model.calls
    finally:
        restarted.store.close()


def test_completed_commitment_is_not_delivered_until_outbox_ack_and_never_claims_phone(core):
    goal_id = setup_goal(core, commitment=True)
    step = core.goals.claim()
    core.goals.finish(step, result_for(core, goal_id))
    completed = core.goals.recent()[0]
    assert completed["state"] == "DONE" and completed["result"]["result_status"] == "FETCHED"
    assert completed["result"]["source"] == "observation.simulation"
    assert completed["delivery"] is None
    assert completed["commitment_state"] == "IN_PROGRESS"
    job = core.store.one("SELECT job_id FROM jobs WHERE kind='activity' AND state='QUEUED'")
    assert job and run(core.run_once(job_id=job["job_id"]))
    for _ in range(2):
        assert core.goals.recent()[0]["commitment_state"] == "IN_PROGRESS"
        item = lease_deliveries(
            core.store, core.settings, LeaseRequest(worker_id="fixture"), core.clock()
        )["items"][0]
        begin_send(core.store, item["message_id"], item["lease_token"], core.clock(), core.settings)
        delivery_result(
            core.store, core.settings, item["message_id"],
            DeliveryResult(lease_token=item["lease_token"], result="SENT"), core.clock(),
        )
    sent = core.goals.recent()[0]
    assert sent["delivery"]["state"] == "SENT" and sent["delivery"]["sent_at"]
    assert sent["commitment_state"] == "FULFILLED" and sent["phone_confirmation"] is None


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


def queued_share(core):
    source = source_with_older_parent(core)
    core.social.consider(source)
    assert core.social.flush()
    job = core.store.one("SELECT job_id FROM jobs WHERE kind='activity' AND state='QUEUED'")
    assert job and run(core.run_once("share-wording", job_id=job["job_id"]))
    return core.store.one("SELECT * FROM outbox")


def test_upstream_expiry_blocks_goal_acceptance_and_retracts_the_native_view(core, tmp_path):
    source = source_with_older_parent(core)
    proposal = GoalProposal(
        topic="下一步天文阅读", source_event_ids=[source],
        scope=GoalScope(operation="read_content", url=URL),
    )
    goal_id = core.goals.propose(proposal)
    key, archive, dest = Fernet.generate_key(), tmp_path / "goal.enc", tmp_path / "restore.db"
    backup(core.settings.database, archive, key)
    core.clock.advance(2)
    with pytest.raises(CoreError, match="GOAL_SOURCE_UNAVAILABLE"):
        core.goals.accept(goal_id)
    assert core.goals.recent() == []
    assert core.store.one("SELECT redacted FROM events WHERE event_id=?", (source,))["redacted"]
    assert core.store.rows("SELECT * FROM goal_steps") == []
    restore(archive, dest, key, core.settings.erasure_ledger)
    restored = Core(core.settings.model_copy(update={"database": str(dest)}), clock=core.clock)
    try:
        assert restored.goals.recent() == []
    finally:
        restored.store.close()


@pytest.mark.parametrize("stage", ["consider", "flush", "lease", "begin"])
def test_share_rechecks_transitive_expiry_at_every_boundary_without_background_worker(core, stage):
    source = source_with_older_parent(core)
    shadow = core.social.preview(source, core.settings.owner_qq_id)
    assert shadow["mode"] == "SHADOW" and shadow["auto_share"] is False
    if stage != "consider":
        core.social.consider(source)
    if stage in {"lease", "begin"}:
        assert core.social.flush()
        job = core.store.one("SELECT job_id FROM jobs WHERE kind='activity' AND state='QUEUED'")
        assert job and run(core.run_once("share-wording", job_id=job["job_id"]))
    if stage == "begin":
        item = lease_deliveries(
            core.store, core.settings, LeaseRequest(worker_id="fixture"), core.clock()
        )["items"][0]
    core.clock.advance(2)
    assert core.social.preview(source, core.settings.owner_qq_id) is None
    if stage == "consider":
        assert core.social.consider(source) is None
        assert not core.store.rows("SELECT * FROM social_candidates")
    elif stage == "flush":
        assert not core.social.flush() and not core.store.rows("SELECT * FROM outbox")
    elif stage == "lease":
        assert not lease_deliveries(
            core.store, core.settings, LeaseRequest(worker_id="fixture"), core.clock()
        )["items"]
    else:
        with pytest.raises(CoreError, match="NOTICE_SOURCE_EXPIRED"):
            begin_send(
                core.store, item["message_id"], item["lease_token"], core.clock(), core.settings
            )
    for row in core.store.rows("SELECT * FROM outbox"):
        assert row["state"] == "CANCELLED" and row["attempt_count"] == 0
    assert len(core.model.calls) == (1 if stage in {"lease", "begin"} else 0)


@pytest.mark.parametrize(
    "mutation", ["source", "candidate", "content", "disabled", "action", "origin", "recipient"]
)
def test_share_final_dispatch_rejects_changed_source_candidate_content_and_disabled_scope(
    core, mutation
):
    queued = queued_share(core)
    item = lease_deliveries(
        core.store, core.settings, LeaseRequest(worker_id="fixture"), core.clock()
    )["items"][0]
    if mutation == "source":
        core.store.execute("UPDATE observations SET source_sha256=?", ("0" * 64,))
    elif mutation == "candidate":
        core.store.execute("UPDATE social_candidates SET state='SKIPPED'")
    elif mutation == "content":
        core.store.execute("UPDATE outbox SET content_json=?", (encode({"text": "被替换的正文"}),))
    elif mutation == "disabled":
        core.settings.social_enabled = False
    elif mutation == "action":
        core.store.execute("UPDATE outbox SET action_type='qq.private.reply'")
    elif mutation == "origin":
        core.store.execute("UPDATE outbox SET origin_event_id='evt_test_1'")
    else:
        core.store.execute("UPDATE conversations SET scope='group'")
    with pytest.raises(CoreError, match="NOTICE_"):
        begin_send(core.store, item["message_id"], item["lease_token"], core.clock(), core.settings)
    result = core.store.one("SELECT * FROM outbox WHERE message_id=?", (queued["message_id"],))
    assert result["state"] == "CANCELLED" and result["attempt_count"] == 0
    restarted = Core(core.settings, clock=core.clock)
    try:
        assert not lease_deliveries(
            restarted.store, restarted.settings, LeaseRequest(worker_id="fixture"), core.clock()
        )["items"]
    finally:
        restarted.store.close()


@pytest.mark.parametrize("uncertain", [False, True])
def test_source_expiry_never_claims_cancellation_after_io_started(core, uncertain):
    queued_share(core)
    item = lease_deliveries(
        core.store, core.settings, LeaseRequest(worker_id="fixture"), core.clock()
    )["items"][0]
    begin_send(core.store, item["message_id"], item["lease_token"], core.clock(), core.settings)
    if uncertain:
        delivery_result(
            core.store, core.settings, item["message_id"],
            DeliveryResult(lease_token=item["lease_token"], result="DELIVERY_UNCERTAIN"),
            core.clock(),
        )
    core.clock.advance(2)
    assert not lease_deliveries(
        core.store, core.settings, LeaseRequest(worker_id="fixture"), core.clock()
    )["items"]
    row = core.store.one("SELECT * FROM outbox")
    assert row["state"] == ("DELIVERY_UNCERTAIN" if uncertain else "SENDING")
    assert row["attempt_count"] == 1


def test_social_expiry_gate_does_not_change_ordinary_owner_reply(core):
    ingest(core)
    event = core.store.one("SELECT * FROM events")
    message = enqueue(
        core.store, event["event_id"], event["conversation_id"], "普通回复", core.clock()
    )
    item = lease_deliveries(
        core.store, core.settings, LeaseRequest(worker_id="fixture"), core.clock()
    )["items"][0]
    assert begin_send(core.store, message, item["lease_token"], core.clock(), core.settings) == {
        "state": "SENDING"
    }


def test_interest_or_goal_does_not_substitute_for_reader_grant(core):
    prepare(core)
    with core.store.transaction():
        request = core.exploration.create("heybox", "read", "no-grant", URL)
    before = core.store.db.total_changes
    with pytest.raises(CoreError, match="SOURCE_GRANT_REQUIRED"):
        core.exploration.lease("heybox")
    row = core.store.one("SELECT * FROM exploration_requests WHERE request_id=?", (request,))
    assert row["state"] == "QUEUED" and row["started_at"] is None
    assert core.store.db.total_changes == before
    assert not core.store.rows("SELECT * FROM outbox") and not core.model.calls


@pytest.mark.parametrize("view", ["goals", "reflections"])
def test_private_life_views_require_control_auth_never_cache_and_retract_on_forget(core, view):
    if view == "goals":
        setup_goal(core)
    else:
        enable_nightly(core)
        assert run(core.reflections.run_day(DAY))["state"] == "COMMITTED"
    core.settings.control_token = SecretStr("owner-control-" * 4)
    with TestClient(create_app(core.settings)) as client:
        api_core = client.app.state.core
        api_core.clock = api_core.goals.clock = api_core.reflections.clock = core.clock
        bridge = {"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()}
        owner = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
        path = "/internal/v2/" + view
        assert client.get(path, headers=bridge).status_code == 401
        before = client.get(path, headers=owner)
        assert before.status_code == 200 and before.json()["items"]
        assert before.headers["Cache-Control"] == "no-store"
        with core.store.transaction():
            core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
        after = client.get(path, headers=owner)
        assert after.status_code == 200 and after.json()["items"] == []
        assert after.headers["Cache-Control"] == "no-store"
