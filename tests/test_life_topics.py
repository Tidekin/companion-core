"""Approved Life metadata and original activity execution, all inputs synthetic."""

import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from companion.api import create_app
from companion.backup import backup, restore
from companion.contracts import CoreError
from companion.core import Core
from companion.life_protocol import LifeLoopConfig
from companion.store import encode
from companion.transport import enqueue
from tests.conftest import ingest, run
from tests.support.life_topics import assert_no_delivery, bug, configure, due, proposal


def test_off_and_empty_short_circuit_before_model(core):
    ingest(core)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    assert core.life_topics.schedule() is None
    assert core.store.rows("SELECT * FROM life_topics") == []
    configure(core)
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    assert core.life_topics.schedule() is None
    assert not core.model.calls
    assert_no_delivery(core)


def test_unresolved_bug_next_day_one_shadow_and_no_repeat_after_restart(core):
    topic = bug(core)
    assert topic["state"] == "WAITING"
    assert core.life_topics.schedule() is None
    proposal(core)
    job = due(core)
    assert job
    assert run(core.run_once("life-test", job_id=job))
    result = core.store.one("SELECT * FROM life_activity_results")
    assert result["state"] == "SHADOW", core.store.rows("SELECT * FROM jobs")
    assert result["action"] == "FOLLOW_UP"
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    assert core.store.one("SELECT state FROM life_topics")["state"] == "CLOSED"
    core.clock.advance(86400)
    with Core(core.settings, core.model, core.clock) as restarted:
        assert restarted.life_topics.schedule() is None
        assert restarted.life_topics.preview(core.settings.owner_qq_id)["items"][0]["results"]
    assert len(core.model.calls) == 1
    assert_no_delivery(core)


@pytest.mark.parametrize("when", ["before", "during"])
def test_explicit_close_stays_terminal_and_rejects_late_result(core, when):
    topic = bug(core)
    proposal(core)
    if when == "before":
        core.life_topics.close(topic["topic_id"], "evt_test_1")
        assert due(core) is None
    else:
        job = due(core)
        core.model.callback = lambda: core.life_topics.close(topic["topic_id"], "evt_test_1")
        assert run(core.run_once(job_id=job))
    assert core.store.one("SELECT state FROM life_topics")["state"] == "CLOSED"
    assert not core.store.rows("SELECT * FROM life_activity_results WHERE text<>''")
    assert_no_delivery(core)


@pytest.mark.parametrize("change", ["pause", "off", "delete", "expiry", "new_input"])
def test_inflight_change_rejects_stale_shadow_without_refunding_usage(core, change):
    bug(core)
    proposal(core)
    job = due(core)

    def mutate():
        if change == "pause":
            core.goals.pause_autonomy(True)
        elif change == "off":
            core.settings.life_loop = LifeLoopConfig()
        elif change == "delete":
            with core.store.transaction():
                core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
        elif change == "expiry":
            core.clock.advance(8 * 86400)
        else:
            ingest(core, 2, "现在先聊另一件事")

    core.model.callback = mutate
    assert run(core.run_once(job_id=job))
    assert not core.store.rows("SELECT * FROM life_activity_results WHERE text<>''")
    assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"
    assert_no_delivery(core)
    if change == "new_input":
        core.model.callback = None
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE kind='chat'")
        core.clock.advance(max(3601, core.settings.proactive_idle_seconds + 1))
        reconsider = core.life_topics.schedule()
        assert reconsider and reconsider != job
        assert run(core.run_once(job_id=reconsider))
        assert core.store.one('SELECT state FROM life_activity_results WHERE job_id=%s',
                              (reconsider,))["state"] == "SHADOW"


@pytest.mark.parametrize("destination", ["PRIVATE_DIARY", "PRIVATE_MESSAGE_DRAFT", "QZONE_DRAFT"])
def test_destination_scope_keeps_private_sources_out_of_qzone(core, destination):
    bug(core)
    proposal(core, action="SHARE", destination=destination)
    job = due(core)
    assert run(core.run_once(job_id=job))
    result = core.store.one("SELECT * FROM life_activity_results")
    if destination == "QZONE_DRAFT":
        assert not result["text"]
        assert (
            core.store.one('SELECT last_error_code FROM jobs WHERE job_id=%s', (job,))[
                "last_error_code"
            ]
            == "LIFE_PROPOSAL_SCOPE_REQUIRED"
        )
    else:
        assert result["state"] == "SHADOW"
        assert json.loads(result["proposal_json"])["destination"] == destination
    assert_no_delivery(core)


def test_duplicate_workers_claim_original_job_once(core):
    bug(core)
    proposal(core)
    job = due(core)
    assert core.life_topics.schedule() is None
    with Core(core.settings, core.model, core.clock) as second:
        assert second.claim_job("second", job_id=job)
        assert core.claim_job("first", job_id=job) is None
    assert_no_delivery(core)


def test_budget_denial_defers_unadmitted_job_without_model_call(core):
    bug(core)
    proposal(core)
    core.settings.autonomous_daily_micro_usd = 0
    job = due(core)
    assert run(core.run_once(job_id=job))
    assert not core.model.calls
    assert not core.store.rows("SELECT * FROM model_usage")
    assert not core.store.rows("SELECT * FROM life_activity_results WHERE text<>''")
    assert_no_delivery(core)


def test_preview_is_uncached_authorized_and_read_only_before_any_topic_write(core):
    configure(core, "PREVIEW")
    ingest(core)
    core.settings.control_token = SecretStr("synthetic-preview-control-" * 3)
    app = create_app(core.settings)
    app.state.core = core
    client = TestClient(app)
    before = core.store.db.total_changes
    assert client.get("/internal/v2/life/topics").status_code == 401
    result = client.get(
        "/internal/v2/life/topics",
        headers={
            "Authorization": "Bearer " + core.settings.control_token.get_secret_value(),
        },
    )
    assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
    assert result.json()["candidate_sources"][0]["event_id"] == "evt_test_1"
    assert result.json()["items"] == []
    assert core.store.db.total_changes == before
    assert_no_delivery(core)


def test_owner_end_command_never_becomes_a_new_topic(core):
    topic = bug(core)
    ingest(core, 2, "/不再跟进")
    assert core.life.command("/不再跟进", "evt_test_2").startswith("已结束")
    core.store.execute("UPDATE jobs SET state='CANCELLED'")
    assert due(core) is None
    assert core.store.one("SELECT * FROM life_topics")["topic_id"] == topic["topic_id"]
    assert core.store.one("SELECT COUNT(*) n FROM life_topics")["n"] == 1


def test_owner_close_after_generation_withdraws_the_reviewable_draft(core):
    topic = bug(core)
    proposal(core)
    assert run(core.run_once(job_id=due(core)))
    usage = core.store.rows("SELECT * FROM model_usage")
    core.life_topics.close(topic["topic_id"], "evt_test_1")
    assert not core.life_topics.preview(core.settings.owner_qq_id)["items"][0]["results"]
    assert not core.store.rows("SELECT * FROM life_activity_results WHERE text<>''")
    assert core.store.rows("SELECT * FROM model_usage") == usage


def test_latest_input_cannot_be_silently_omitted_from_bounded_topic(core):
    bug(core)
    for number in range(2, 10):
        ingest(core, number, "还有一条新信息" if number < 9 else "这个问题已经解决了")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.clock.advance(86400)
    assert core.life_topics.schedule() is None
    assert core.life_topics.schedule() is None
    assert not core.model.calls
    assert not core.store.rows("SELECT * FROM life_activity_results")


@pytest.mark.parametrize("action", ["SKIP", "EXPLORE", "DEFER"])
def test_bounded_choices_do_not_dispatch_and_defer_cannot_loop_forever(core, action):
    bug(core)
    extra = {"defer_seconds": 60} if action == "DEFER" else {}
    proposal(core, action, "NONE", text="", **extra)
    job = due(core)
    assert run(core.run_once(job_id=job))
    assert core.store.one("SELECT action FROM life_activity_results")["action"] == action
    if action == "DEFER":
        core.clock.advance(3601)
        second = core.life_topics.schedule()
        assert second and second != job
        assert run(core.run_once(job_id=second))
        core.clock.advance(3601)
        assert core.life_topics.schedule() is None
    if action == "DEFER":
        assert core.store.one("SELECT state,reason_code FROM life_topics") == {
            "state": "WAITING", "reason_code": "WAITING_FOR_NEW_EVIDENCE"}
        ingest(core, 2, "导出时还有一个新线索")
        core.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE kind='chat'")
        assert due(core)
    else:
        assert core.store.one("SELECT state FROM life_topics")["state"] == "CLOSED"
    assert not core.store.rows("SELECT * FROM exploration_requests")
    assert_no_delivery(core)


def test_unknown_model_outcome_and_crashed_lease_never_restart_same_attempt(core):
    bug(core)
    core.model.error = CoreError("MODEL_RETRYABLE", 503)
    job = due(core)
    assert run(core.run_once(job_id=job))
    before = core.store.rows("SELECT * FROM model_usage")
    assert before[0]["state"] == "UNKNOWN"
    core.clock.advance(3601)
    core.model.error = None
    with Core(core.settings, core.model, core.clock) as restarted:
        assert restarted.life_topics.schedule() is None
        assert not run(restarted.run_once(job_id=job))
    assert core.store.rows("SELECT * FROM model_usage") == before
    assert len(core.model.calls) == 1
    assert_no_delivery(core)


def test_expired_running_lease_is_failed_without_paid_replay(core):
    bug(core)
    proposal(core)
    job = due(core)
    assert core.claim_job("crashed-worker", job_id=job)
    core.clock.advance(core.settings.lease_seconds + 1)
    assert not run(core.run_once(job_id=job))
    assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (job,))["state"] == "FAILED"
    assert not core.model.calls


def test_source_changed_after_generation_hides_text_and_erasure_replays_old_backup(core, tmp_path):
    bug(core)
    proposal(core)
    job = due(core)
    assert run(core.run_once(job_id=job))
    key, archive, destination = Fernet.generate_key(), tmp_path / "life.enc", tmp_path / "old.db"
    backup(core.settings.database, archive, key)
    core.store.execute(
        "UPDATE events SET body_json=%s WHERE event_id='evt_test_1'",
        (encode({"text": "来源已改写"}),),
    )
    assert not core.life_topics.preview(core.settings.owner_qq_id)["items"][0]["results"]
    with core.store.transaction():
        core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    restore(archive, destination, key, core.settings.erasure_ledger)
    with Core(
        core.settings.model_copy(update={"database": str(destination)}), clock=core.clock
    ) as old:
        assert not old.store.rows("SELECT * FROM life_activity_results WHERE text<>''")
        assert old.store.one("SELECT state FROM life_topics")["state"] == "EXPIRED"
        assert not old.life_topics.preview(core.settings.owner_qq_id)["items"][0]["sources"]


def test_uncertain_delivery_blocks_new_decision_without_mutating_original_send(core):
    topic = bug(core)
    message = enqueue(core.store, "evt_test_1", topic["conversation_id"], "原投递", core.clock())
    core.store.execute(
        "UPDATE outbox SET state='DELIVERY_UNCERTAIN',attempt_count=1 WHERE message_id=%s",
        (message,),
    )
    original = core.store.one("SELECT * FROM outbox")
    assert due(core) is None
    assert not core.model.calls
    assert core.store.rows("SELECT * FROM life_activity_results") == []
    assert core.store.one("SELECT * FROM outbox") == original


@pytest.mark.parametrize("mode", ["PREVIEW", "SHADOW"])
def test_new_pipeline_does_not_run_legacy_generic_proactive_in_parallel(core, mode):
    from companion.autonomous_selector import native_selector
    from tests.support.autonomous_selector import drain

    configure(core, mode)
    core.settings.proactive_enabled = core.settings.social_enabled = True
    ingest(core)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.clock.advance(3601)
    run(drain(native_selector(core, "life-no-fallback")))
    assert core.social.flush() is False
    assert not core.store.rows("SELECT * FROM jobs WHERE kind='activity'")
    assert not core.model.calls
    assert_no_delivery(core)


@pytest.mark.parametrize("bad", ["group", "other_owner", "quoted", "control"])
def test_ineligible_inputs_never_enter_topic_context(core, bad):
    configure(core)
    ingest(core)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    if bad == "group":
        core.store.execute("UPDATE conversations SET scope='group'")
    elif bad == "other_owner":
        core.store.execute("UPDATE events SET actor_id='system'")
    else:
        body = (
            {"text": "旁人的未解决问题", "rich": ["quoted"]}
            if bad == "quoted"
            else {
                "text": "/不再跟进",
                "control_command": True,
            }
        )
        core.store.execute('UPDATE events SET body_json=%s', (encode(body),))
    assert due(core) is None
    assert not core.store.rows("SELECT * FROM life_topics")
    assert not core.model.calls


def test_long_owner_source_keeps_its_ending_and_decision_sees_current_time(core):
    configure(core)
    ending = "现在已经解决了，不用再跟进。"
    text = "导出时一直报错，" + "日志里反复出现同一个超时。" * 43 + ending
    assert len(text) > 500
    ingest(core, 1, text)
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    core.life_topics.synchronize()
    proposal(core, action="SKIP", destination="NONE", text="")
    job = due(core)
    assert job and run(core.run_once("life-test", job_id=job))

    messages = core.model.calls[-1][0]
    material = json.loads(messages[-1]["content"])
    source = material["sources"][0]
    assert source["text"].startswith("导出时一直报错") and source["text"].endswith(ending)
    assert "…" in source["text"] and len(source["text"]) == 500
    assert source["text_truncated"] is True
    clock = [m["content"] for m in messages if m["content"].startswith("当前时间参考")]
    assert len(clock) == 1 and "本次生活判断的时间" in clock[0]
    # The moving decision time is prompt context only, never part of the frozen material.
    frozen = json.loads(core.store.one(
        'SELECT body_json FROM events e JOIN jobs j USING(event_id) WHERE j.job_id=%s',
        (job,))["body_json"])["life_material"]
    assert frozen["sources"][0]["occurred_at"] == core.store.one(
        "SELECT occurred_at FROM events WHERE event_id='evt_test_1'")["occurred_at"]


def test_unavailable_due_candidates_cannot_hide_a_later_one(core, monkeypatch):
    topic = bug(core)
    proposal(core)
    core.clock.advance(86400)
    core.life_topics.synchronize()
    assert core.store.one("SELECT state FROM life_topics")["state"] == "DUE"
    # Twenty earlier synthetic candidates whose material is unavailable this round.
    for index in range(20):
        core.store.execute(
            (
                "INSERT INTO life_topics SELECT %s,conversation_id,%s,'readin"
                "g','DUE',%s,expires_at,reason_code,revision,0,NULL,created_a"
                't,updated_at FROM life_topics WHERE topic_id=%s'
            ),
            (f"blocked_{index:02d}", f"blocked:{index}", f"2000-01-01T00:00:{index:02d}+00:00",
             topic["topic_id"]))
    monkeypatch.setattr(core.life_topics, "synchronize", lambda: None)
    material = core.life_topics.material
    checked = []

    def blocked(topic_id, **options):
        checked.append(topic_id)
        if topic_id.startswith("blocked_"):
            raise CoreError("LIFE_SOURCE_UNAVAILABLE", 409)
        return material(topic_id, **options)

    monkeypatch.setattr(core.life_topics, "material", blocked)
    assert core.life_topics.schedule() is None
    assert len(checked) == 20 and all(item.startswith("blocked_") for item in checked)
    job = core.life_topics.schedule()
    assert job and checked[-1] == topic["topic_id"]
    assert core.store.one('SELECT topic_id FROM life_activity_results WHERE job_id=%s',
                          (job,))["topic_id"] == topic["topic_id"]
