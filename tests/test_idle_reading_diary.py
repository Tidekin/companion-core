"""A quiet day through native scheduling, loopback Reader HTTP, judgement and Diary."""

import json
from datetime import timedelta

import httpx
import pytest

from companion.autonomous_selector import native_selector
from companion.contracts import CoreError
from companion.core import Core
from companion.life_protocol import LifeLoopConfig, LifeShadowApproval
from companion.model import ModelClient
from companion.model_carry import purpose_revision
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.store import encode
from source_reader.bilibili import BilibiliReader
from source_reader.network import PublicHTTP
from source_reader.worker_v1 import tick
from tests.conftest import ingest, run
from tests.support.core_http import real_core_http
from tests.support.exploration import configure
from tests.support.heybox_reader import mock_pool
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare
from tests.support.social_owner_authority import stop
from tests.support.source_reader import META, PLAYER, SUBTITLE, response
from tests.support.topic_selection import judged


def enable(core):
    configure(core)
    cfg = core.settings
    cfg.wake_enabled = cfg.content_enabled = cfg.diary_enabled = True
    cfg.watch_ledger.enabled = True
    cfg.life_loop = LifeLoopConfig(mode="SHADOW", read_goals_enabled=True, autonomy_enabled=True)
    cfg.quiet_start = cfg.quiet_end = 0
    cfg.observation_adapters["bili"].reader_max_requests = 4
    cfg.exploration_daily_requests = 20


def test_reviewed_reading_share_revision_changes_only_source_permission():
    scope = LifeShadowApproval(scope="owner_private_life_shadow_v1", authorization_sha256="b"*64,
        conversation_id="private-owner", source_event_ids=["selected"],
        purposes=["PRIVATE_MESSAGE_DRAFT"], expires_at="2027-01-01T00:00:00Z", max_items=4)
    old = OwnerGrantPolicy(owner_qq_id="10001", purposes=["life", "proactive"], life_shadow=scope,
                          authorization_sha256="c"*64, paid_contract_sha256="d"*64)
    enabled = scope.model_copy(update={"share_own_readings": True})
    new = old.model_copy(update={"life_shadow": enabled})
    purpose_revision(old, new)
    purpose_revision(new, old)
    for other in ({"max_items": 5}, {"conversation_id": "elsewhere"},
                  {"automatic_actions": ["qq.private.proactive"]}):
        with pytest.raises(ValueError):
            purpose_revision(old, new.model_copy(update={"life_shadow": enabled.model_copy(
                update=other)}))


async def read_and_judge(core, lanes):
    assert (await lanes["wake"].run()).progressed
    request = core.store.one("SELECT * FROM exploration_requests")
    assert (request["adapter_id"], request["query"]) == ("bili", "恒星")
    assert not (await lanes["wake"].run()).progressed
    # Ten earlier titles would previously crowd the substantive read out of Diary.
    listing = [{"type": "video", "title": "恒星图谱", "bvid": f"BV1{x:08d}a"}
               for x in range(12)]
    pool, backend = mock_pool(response({"code": 0, "data": {"result": listing}}),
                              response(META), response(PLAYER), response(SUBTITLE))
    auth = {"Authorization": "Bearer " + core.settings.observation_adapters["bili"]
            .token.get_secret_value()}
    async with (
        real_core_http(core) as port,
        PublicHTTP(pool) as public,
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=auth,
                          trust_env=False) as client
    ):
        reader = BilibiliReader(public, clock=core.clock)
        assert await tick(client, reader, "bili", source="bilibili",
                          clock=lambda: core.clock.now)
        assert not (await lanes["goal"].run()).progressed
        assert core.store.one("SELECT count(*) n FROM goals")["n"] == 1
        core.clock.advance(core.settings.wake_min_interval_minutes * 60 + 1)
        assert (await lanes["wake"].run()).progressed
        assert (await lanes["goal"].run()).progressed
        assert await tick(client, reader, "bili", source="bilibili",
                          clock=lambda: core.clock.now)
    assert len(backend.requests) == 4
    assert core.store.one("SELECT 1 FROM goals WHERE state='DONE'")
    assert (await lanes["watch"].run()).progressed


@pytest.mark.parametrize("worthy", [True, False])
def test_no_chat_interest_read_can_become_a_private_diary(core, tmp_path, worthy):
    enable(core)
    cfg = core.settings
    judged(core, 1, ["恒星"], 2)
    core.clock.advance(86400)  # The previous judgement is an interest, not today's evidence.
    day = core.clock.now.date()
    policy = prepare(core, tmp_path)
    scope = LifeShadowApproval(scope="owner_private_life_shadow_v1",
        authorization_sha256="e" * 64, conversation_id="synthetic-private",
        source_event_ids=["watch_obs_1"], purposes=["PRIVATE_DIARY"],
        expires_at=core.clock.now + timedelta(days=4), max_items=2)
    purposes = ["wake", "life", "goals", "exploration", "content", "diary"]
    activate(core, policy.model_copy(update={"purposes": purposes, "life_shadow": scope}))
    calls = []
    take = "原来颜色也能透露温度。"
    quote = "恒星的颜色与温度有关。"

    def provider(request):
        data = json.loads(json.loads(request.content)["messages"][-1]["content"])
        if "sources" not in data:
            calls.append("watch")
            answer = {"rating": 2, "take": take, "topic_tags": ["恒星"]}
        else:
            calls.append("diary")
            sources = {row["kind"]: row for row in data["sources"]}
            assert set(sources) == {"observed_content", "own_take"}
            read, view = sources["observed_content"], sources["own_take"]
            assert read["result_status"] == "FETCHED" and quote in read["text"]
            assert take in view["text"]
            answer = {"schema_version": 1, "worth_saving": worthy}
            if worthy:
                answer.update(local_date=day.isoformat(), title="恒星的颜色", sections=[
                    {"kind": "observation", "text": quote, "evidence": [
                        {"source_id": read["source_id"], "exact_quote": quote}]},
                    {"kind": "reflection", "text": take, "evidence": [
                        {"source_id": view["source_id"], "exact_quote": take}]}])
        return httpx.Response(200, json={"choices": [{"message": {"content": encode(answer)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model_http:
            with Core(cfg, ModelClient(cfg, model_http), core.clock) as worker:
                lanes = {lane.name: lane for lane in native_selector(worker, "quiet-day").lanes}
                await read_and_judge(core, lanes)
                core.clock.advance(86400)
                worker.diary.schedule_day(day)
                assert (await lanes["diary"].run()).progressed
                assert not (await lanes["diary"].run()).progressed

    run(scenario())
    assert calls == ["watch", "diary"]
    assert not core.store.one("SELECT 1 FROM outbox")
    assert not core.store.one("SELECT 1 FROM events WHERE actor_id='owner'")
    entry = core.diary.get_day(day, cfg.owner_qq_id)
    if worthy:
        assert quote in entry["body_md"] and take in entry["body_md"]
        source = core.store.one("SELECT result_event_id FROM goals WHERE state='DONE'")
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, [source["result_event_id"]], core.clock())
        assert core.diary.get_day(day, cfg.owner_qq_id) is None
    else:
        assert entry is None


def test_idle_wake_yields_to_recent_owner_input_and_quiet_hours(core):
    enable(core)
    ingest(core, 1, "聊聊昨天的事")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    assert not core.wake.evaluate()
    core.clock.advance(core.settings.proactive_idle_seconds + 1)
    core.settings.quiet_start, core.settings.quiet_end = 8, 20
    assert not core.wake.evaluate()
    assert not core.store.one("SELECT 1 FROM exploration_requests")


@pytest.mark.parametrize(("opt_in", "rating", "choice"), [
    (False, 2, None), (True, 0, None), (True, 1, "SHARE"), (True, 2, "SKIP"),
    (True, 1, "DEFER"),
])
def test_self_read_requires_consent_positive_judgement_and_valid_choice(
    core, tmp_path, opt_in, rating, choice,
):
    enable(core)
    cfg = core.settings
    cfg.life_loop.max_daily_decisions = 4
    cfg.proactive_enabled = True
    cfg.life_loop.min_interval_seconds = 10800
    judged(core, 1, ["恒星"], 2)
    core.clock.advance(86400)
    policy = prepare(core, tmp_path)
    target = "private-reading-test"
    core.store.insert("conversations", conversation_id=target, platform="native.chat",
                      platform_session_key="native:owner", scope="private", owner_actor_id="owner",
                      created_at=core.clock())
    scope = LifeShadowApproval(scope="owner_private_life_shadow_v1", authorization_sha256="e"*64,
        conversation_id=target, source_event_ids=["watch_obs_1"],
        purposes=["PRIVATE_MESSAGE_DRAFT", "PRIVATE_MOMENT"], share_own_readings=opt_in,
        automatic_actions=["qq.private.proactive"],
        expires_at=core.clock.now + timedelta(days=4), max_items=4)
    assert ("share_own_readings" in scope.model_dump(mode="json")) is opt_in
    purposes = ["wake", "life", "goals", "exploration", "content", "proactive", "moments"]
    active, _ = activate(core, policy.model_copy(update={
        "purposes": purposes, "life_shadow": scope}))
    decisions = []

    def provider(request):
        data = json.loads(json.loads(request.content)["messages"][-1]["content"])
        if data.get("kind") == "life":
            decisions.append(data)
            assert data["allowed_actions"] == ["SHARE", "SKIP"]
            assert data["allowed_destinations"] == ["PRIVATE_MESSAGE_DRAFT"]
            read, = data["sources"]
            assert read["own_reading"]["take"] == "颜色可以透露温度。"
            answer = {"action": choice, "reason": "真实读后判断", "source_event_ids": []}
            if choice == "SHARE":
                answer.update(destination="PRIVATE_MESSAGE_DRAFT", text="颜色还能透露恒星的温度。",
                              source_event_ids=[read["event_id"]])
            elif choice == "DEFER":
                answer["defer_seconds"] = 600
        else:
            answer = {"rating": rating, "take": "颜色可以透露温度。", "topic_tags": ["恒星"]}
        return httpx.Response(200, json={"choices": [{"message": {"content": encode(answer)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            with Core(cfg, ModelClient(cfg, http), core.clock) as worker:
                lanes = {lane.name: lane for lane in native_selector(worker, "read-share").lanes}
                await read_and_judge(core, lanes)
                job = worker.life_topics.schedule()
                assert bool(job) == bool(choice)
                if job:
                    assert (await lanes["activity"].run()).progressed
                    result = core.store.one("SELECT * FROM life_activity_results WHERE job_id=%s",
                                            (job,))
                    if choice == "DEFER":
                        error = core.store.one("SELECT last_error_code FROM jobs WHERE job_id=%s",
                                               (job,))
                        assert error["last_error_code"] == "LIFE_PROPOSAL_SCOPE_REQUIRED"
                    else:
                        assert result["action"] == choice
                        core.clock.advance(cfg.life_loop.min_interval_seconds)
                        assert not worker.life_topics.schedule()
                    if choice == "SHARE":
                        retained = worker.life_topics.retained_result(job)
                        assert retained["topic"]["conversation_id"] == target
                        # Aborting this test transaction restores the original review.
                        with pytest.raises(CoreError, match="LIFE_SOURCE_UNAVAILABLE"):
                            with core.store.transaction():
                                core.store.execute(
                                    "UPDATE watch_ledger SET rating=0 WHERE source_event_id=%s",
                                    (decisions[0]["sources"][0]["event_id"],))
                                core.life_topics.retained_result(job)
                        assert worker.life_topics.consume_due()
                        assert not worker.life_topics.consume_due()
                        sent = core.store.rows(
                            "SELECT action_type,state,conversation_id FROM outbox")
                        assert sent == [{"action_type": "native.private.proactive", "state": "SENT",
                                         "conversation_id": target}]
                        stop(core, active["authority"])
                        with pytest.raises(CoreError):
                            worker.life_topics.retained_result(job)
                assert len(decisions) == int(bool(choice))
                if choice != "SHARE":
                    assert not core.store.one("SELECT 1 FROM outbox")
    run(scenario())


def test_open_topic_has_priority_and_pause_and_daily_limit_still_apply(core):
    from tests.support.life_topics import bug

    topic = bug(core)
    enable(core)
    core.clock.advance(core.settings.proactive_idle_seconds + 1)
    assert topic and not core.wake.evaluate()
    core.store.execute("UPDATE life_topics SET state='CLOSED'")
    core.store.execute(
        "INSERT INTO runtime_meta (key,value) VALUES ('life_paused','1') ON "
        'CONFLICT(key) DO UPDATE SET '
        'value=excluded.value,record_sequence=DEFAULT'
    )
    assert not core.wake.evaluate()
    core.store.execute("DELETE FROM runtime_meta WHERE key='life_paused'")
    core.clock.advance(core.settings.wake_min_interval_minutes * 60 + 1)
    core.settings.wake_max_daily_runs = 0
    assert not core.wake.evaluate()
    assert core.store.one(
        'SELECT reason_code FROM wake_decisions ORDER BY record_sequence DESC'
        ' LIMIT 1'
    ) == {
        "reason_code": "DAILY_LIMIT"}


def test_fresh_judged_read_competes_with_old_chat_for_one_shared_slot(core, tmp_path):
    enable(core)
    cfg, s = core.settings, core.store
    cfg.life_loop.max_daily_decisions = 1
    ingest(core, 41, "这个问题还有待确认。")
    s.execute("UPDATE jobs SET state='CANCELLED'")
    target = s.one("SELECT conversation_id FROM events WHERE event_id='evt_test_41'")[
        "conversation_id"]
    core.clock.advance(2 * 86400)
    url = "https://www.bilibili.com/video/BV100000042a"
    event = judged(core, 42, ["恒星"], 1, url=url)
    s.insert("model_usage", usage_id="review-42", request_id="watch-42", scenario="watch_review",
             provider="fixture", model="fixture", occurred_at=core.clock(), state="SETTLED")
    s.execute("UPDATE watch_ledger SET model_request_id='watch-42' WHERE source_event_id=%s",
              (event,))
    s.insert("goals", goal_id="read-42", kind="self", topic="恒星",
             scope_json=encode({"operation": "read_content", "url": url}), proposal_key="read-42",
             state="DONE", priority=1, next_due_at=core.clock(), result_event_id=event,
             created_at=core.clock(), updated_at=core.clock())
    scope = LifeShadowApproval(scope="owner_private_life_shadow_v1", authorization_sha256="e"*64,
        conversation_id=target, source_event_ids=["evt_test_41"], share_own_readings=True,
        purposes=["PRIVATE_MESSAGE_DRAFT"], expires_at=core.clock.now + timedelta(days=2),
        max_items=4)
    policy = prepare(core, tmp_path).model_copy(update={"purposes": ["life"], "life_shadow": scope})
    activate(core, policy)
    s.insert("goal_evidence", goal_id="read-42", event_id="evt_test_41")
    core.life_topics.synchronize()
    assert not s.one("SELECT 1 FROM life_topic_sources WHERE event_id=%s", (event,))
    s.execute("DELETE FROM goal_evidence WHERE goal_id='read-42'")
    job = core.life_topics.schedule()
    row = s.one("SELECT t.* FROM life_topics t JOIN life_activity_results r USING(topic_id) "
                "WHERE r.job_id=%s", (job,))
    assert row["kind"] == "reading"
    assert s.one("SELECT 1 FROM life_topics WHERE kind='conversation' AND state='DUE'")
    core.clock.advance(cfg.life_loop.min_interval_seconds)
    assert core.life_topics.schedule() is None
    assert s.one("SELECT count(*) n FROM life_activity_results")["n"] == 1
