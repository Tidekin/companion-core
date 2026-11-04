"""Life choices reuse the durable native goal queue without delivery authority."""

import json
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from companion import owner_authority
from companion.config import ObservationAdapterConfig
from companion.contracts import CoreError
from companion.core import Core
from companion.exploration import ReadResult
from companion.goals import GoalProposal
from companion.life_protocol import LifeLoopConfig, LifeShadowApproval
from companion.life_topics import _source, authority
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.store import encode
from source_reader.bilibili import BilibiliReader
from source_reader.network import PublicHTTP
from source_reader.web import WebHTTP
from source_reader.worker_v1 import tick
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.owner_authority import activate
from tests.support.owner_memory import environment
from tests.test_exploration import URL, configure
from tests.test_heybox_reader import mock_pool
from tests.test_life_topics import assert_no_delivery, bug, due, proposal
from tests.test_life_worker_integration import native_worker
from tests.test_next_reading import WEB, web_response
from tests.test_source_reader import META, PLAYER, SUBTITLE, response


@pytest.mark.parametrize("action", ["CONTINUE", "EXPLORE"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("source", ["platform", "public_web"])
def test_choice_submits_native_goal_once(core, action, enabled, source):
    bug(core)
    configure(core)
    url = URL
    if source == "public_web":
        core.settings.web_reading_enabled = True
        core.settings.web_reading_hosts = ("*",)
        core.settings.observation_adapters["web"] = ObservationAdapterConfig(
            source="web", token="synthetic-web-reader-token-" * 2,
        )
        url = "https://new-public.example/story"
    core.settings.life_loop.read_goals_enabled = enabled
    native = GoalProposal(
        topic="阅读游戏介绍",
        source_event_ids=["evt_test_1"],
        scope={"operation": "read_content", "url": url},
    )
    extra = (
        {"goal_id": core.goals.propose(native)}
        if action == "CONTINUE"
        else {"goal": native.model_dump(mode="json")}
    )
    proposal(core, action=action, destination="NONE", text="", **extra)
    job = due(core)
    assert job
    assert run(core.run_once(job_id=job))
    result = core.store.one("SELECT * FROM life_activity_results WHERE job_id=?", (job,))
    assert result["state"] == "SHADOW", core.store.rows("SELECT * FROM jobs")
    assert bool(result["goal_id"]) == enabled
    if enabled:
        goal = core.goals.get(result["goal_id"])
        assert goal["state"] == "QUEUED"
        assert core.exploration.schedule_goal()
        assert len(core.store.rows("SELECT * FROM exploration_requests")) == 1
        with Core(core.settings, core.model, core.clock) as restarted:
            assert not run(restarted.run_once(job_id=job))
            assert not restarted.exploration.schedule_goal()
        assert len(core.store.rows("SELECT * FROM goal_steps")) == 1
        assert len(core.store.rows("SELECT * FROM exploration_requests")) == 1
    else:
        assert not core.store.rows("SELECT * FROM goal_steps")
    assert_no_delivery(core)


@pytest.mark.parametrize("source_scope", ["selected", "owner_private_history"])
@pytest.mark.parametrize("reading_source", ["bilibili", "web"])
def test_live_authority_full_loop_independent_workers(
    core, tmp_path, monkeypatch, source_scope, reading_source,
):
    configure(core)
    core.settings.observation_adapters["bili"].reader_max_requests = 4
    core.settings.life_loop = LifeLoopConfig(mode="SHADOW", read_goals_enabled=True)
    core.settings.control_token = SecretStr("synthetic-life-goal-control" * 2)
    core.settings.wake_enabled = True
    core.settings.quiet_start = core.settings.quiet_end = 0
    core.settings.content_enabled = True
    core.settings.rich_input_scope = "references_v1"
    core.settings.extraction_min_events = 100
    core.settings.diary_min_source_events = 100
    core.settings.reflection_min_sources = 100
    target = URL
    if reading_source == "web":
        target = WEB + "?page=2"
        core.settings.web_reading_enabled = True
        core.settings.web_reading_hosts = ("*",)
        core.settings.observation_adapters["web"] = ObservationAdapterConfig(
            source="web", token="synthetic-life-web-reader-token-" * 2, reader_max_requests=4)
    calls = []

    def model(request):
        material = json.loads(json.loads(request.content)["messages"][-1]["content"])
        assert material["kind"] == "life"
        calls.append(material)
        ids = [source["event_id"] for source in material["sources"]]
        choice = (
            {
                "action": "EXPLORE",
                "destination": "NONE",
                "source_event_ids": ids,
                "reason": "查看原文",
                "goal": {
                    "topic": "阅读星空介绍",
                    "source_event_ids": ids,
                    "scope": {"operation": "read_content", "url": target},
                },
            }
            if len(calls) == 1
            else {
                "action": "SHARE",
                "destination": "PRIVATE_MESSAGE_DRAFT",
                "source_event_ids": ids,
                "reason": "实际阅读结果",
                "text": "读到恒星颜色与温度有关。",
            }
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": encode(choice)}}],
                "usage": {"prompt_tokens": 180, "completion_tokens": 60},
            },
        )

    async def exercise():
        async def require_success(response):
            await response.aread()
            assert response.status_code < 400, (
                response.text,
                core.store.rows("SELECT scenario,state FROM model_usage"),
                core.store.rows(
                    "SELECT key,value FROM runtime_meta WHERE key LIKE '%stop%' "
                    "OR key LIKE '%blocked%'"
                ),
            )

        async with environment(core, tmp_path, history=source_scope == "owner_private_history",
            additional_purposes=("goals", "content", "exploration")) as (admit, _, reference):
            inbound, _ = await admit(1, "有空可以看看这个星空介绍，明天聊聊。")
            view = owner_authority.current(core.store, core.settings, core.clock)
            control = {"Authorization": "Bearer " + core.settings.control_token.get_secret_value()}
            async with real_core_http(core) as port, httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False,
                event_hooks={"response": [require_success]},
            ) as client:
                stopped = await client.post("/internal/v1/owner-authority/stop", headers=control,
                    json={"authority": reference, "operation_id": "life-goal-source-complete"})
                assert stopped.status_code == 200
                scope = LifeShadowApproval(scope="owner_private_life_shadow_v1",
                    authorization_sha256="e" * 64,
                    conversation_id=core.store.one("SELECT conversation_id FROM events "
                        "WHERE event_id=?", (inbound.event_id,))["conversation_id"],
                    source_scope=source_scope,
                    source_event_ids=[inbound.event_id] if source_scope == "selected" else [],
                    purposes=["PRIVATE_MESSAGE_DRAFT"],
                    expires_at=core.clock.now + timedelta(days=2), max_items=2)
                policy = OwnerGrantPolicy.model_validate({**view.policy.model_dump(),
                    "purposes": [*view.policy.purposes, "life"], "life_shadow": scope})
                activate(core, policy, revision=stopped.json()["authority"]["revision"],
                         operation="life-goal-explicit-scope")
                core.life_topics.synchronize()
                core.clock.advance(86400)
                await native_worker(core, model, monkeypatch)
                result = core.store.one("SELECT * FROM life_activity_results")
                assert result["goal_id"], core.store.rows("SELECT state,last_error_code FROM jobs")
                with Core(core.settings, clock=core.clock) as goal_worker:
                    assert goal_worker.exploration.schedule_goal()
                adapter = "bili" if reading_source == "bilibili" else "web"
                client.headers["Authorization"] = (
                    "Bearer " + core.settings.observation_adapters[adapter].token.get_secret_value()
                )
                if reading_source == "web":
                    from source_reader import worker_v1

                    pool, backend = mock_pool(web_response())
                    monkeypatch.setattr(worker_v1, "WebHTTP", lambda hosts: WebHTTP(hosts, pool))
                    assert await tick(client, None, adapter, source="web",
                                      clock=lambda: core.clock.now)
                else:
                    pool, backend = mock_pool(response(META), response(PLAYER), response(SUBTITLE))
                    async with PublicHTTP(pool) as public:
                        assert await tick(client, BilibiliReader(public, clock=core.clock),
                            adapter, source="bilibili", clock=lambda: core.clock.now)
                assert len(backend.requests) == (1 if reading_source == "web" else 3)
                goal = core.goals.get(result["goal_id"])
                assert goal["state"] == "DONE"
                assert _source(core.store, core.settings, goal["result_event_id"],
                               core.clock())["kind"] == "observed_content"
                core.clock.advance(3600)
                await native_worker(core, model, monkeypatch)
                shared = core.store.one("SELECT * FROM life_activity_results WHERE action='SHARE'")
                assert shared and shared["state"] == "SHADOW", core.store.rows("SELECT * FROM jobs")
                assert {source["event_id"] for source in calls[1]["sources"]} == {
                    inbound.event_id, goal["result_event_id"]
                }
                assert "QZONE_DRAFT" not in calls[1]["allowed_destinations"]
                assert len(calls) == 2
                assert_no_delivery(core)
                sources = [_source(core.store, core.settings,
                                   goal["result_event_id"], core.clock())]
                scope = policy.life_shadow

                def current_authority():
                    return authority(
                        core.store,
                        core.settings,
                        scope.conversation_id,
                        sources,
                        core.clock(),
                        check_limit=False,
                    )

                assert current_authority()["destinations"] == ["PRIVATE_MESSAGE_DRAFT"]
                if reading_source == "web":
                    source_id, bad_sha = goal["result_event_id"], "f" * 64
                    assert core.social.preview(source_id, core.settings.owner_qq_id) is None
                    for mutation in (
                        "UPDATE observations SET evidence_json='{}'",
                        f"UPDATE observations SET source_sha256='{bad_sha}'",
                        "UPDATE observations SET result_status='DISCOVERED'",
                        "UPDATE observations SET evidence_json=json_set(evidence_json,"
                        "'$.read_observation.provenance.source','qzone')",
                        "UPDATE reading_attempts SET context_json='{}'",
                        "UPDATE reading_attempts SET result_event_ids_json='[]'",
                        f"UPDATE reading_attempts SET original_url_sha256='{bad_sha}'",
                        "UPDATE reading_attempts SET state='UNKNOWN'",
                    ):
                        core.store.execute("SAVEPOINT web_attribution_negative")
                        try:
                            core.store.execute(mutation)
                            with pytest.raises(CoreError, match="LIFE_SOURCE_UNAVAILABLE"):
                                _source(core.store, core.settings, source_id, core.clock())
                        finally:
                            core.store.execute("ROLLBACK TO web_attribution_negative")
                            core.store.execute("RELEASE web_attribution_negative")

                    def visible():
                        return [r for t in core.life_topics.preview(core.settings.owner_qq_id)[
                            "items"] for r in t["results"] if r["action"] == "SHARE"]

                    assert len(visible()) == 1
                    core.store.execute("SAVEPOINT web_metadata_changed")
                    try:
                        core.store.execute("UPDATE observations SET evidence_json=json_set("
                            "evidence_json,'$.read_observation.parser_version','web/changed')")
                        changed = _source(core.store, core.settings, source_id, core.clock())
                        assert changed["source_sha256"] != sources[0]["source_sha256"]
                        assert visible() == []
                    finally:
                        core.store.execute("ROLLBACK TO web_metadata_changed")
                        core.store.execute("RELEASE web_metadata_changed")
                for mutation in (
                    "UPDATE life_activity_results SET goal_id=NULL WHERE action='EXPLORE'",
                    "UPDATE life_activity_results SET authority_sha256='changed' "
                    "WHERE action='EXPLORE'",
                    "UPDATE life_activity_results SET source_fingerprints_json='{}' "
                    "WHERE action='EXPLORE'",
                    "UPDATE goals SET state='FAILED'",
                    "UPDATE goal_steps SET outcome_event_id=NULL",
                    "UPDATE model_usage SET state='UNKNOWN'",
                    "UPDATE events SET body_json='{\"text\":\"changed\"}' "
                    f"WHERE event_id='{inbound.event_id}'",
                    f"UPDATE events SET redacted=1 WHERE event_id='{inbound.event_id}'",
                ):
                    core.store.execute("SAVEPOINT authority_negative")
                    try:
                        core.store.execute(mutation)
                        code = ("MODEL_BUDGET_RECONCILIATION_REQUIRED"
                                if mutation.startswith("UPDATE model_usage")
                                else "LIFE_SHADOW_SCOPE_REQUIRED")
                        with pytest.raises(CoreError, match=code):
                            current_authority()
                    finally:
                        core.store.execute("ROLLBACK TO authority_negative")
                        core.store.execute("RELEASE authority_negative")
                with core.store.transaction():
                    core.ledger.expire_events(core.store, [inbound.event_id], core.clock())
                assert not core.store.rows(
                    "SELECT * FROM life_activity_results WHERE state='SHADOW'")
                with pytest.raises(CoreError, match="GOAL_NOT_FOUND"):
                    core.goals.get(goal["goal_id"])

    run(exercise())


@pytest.mark.parametrize("gate", ["pause", "budget", "adapter", "erasure", "grant"])
def test_native_read_gates_still_block_life_choices(core, gate):
    bug(core)
    configure(core)
    core.settings.life_loop.read_goals_enabled = True
    proposal(
        core,
        action="EXPLORE",
        destination="NONE",
        text="",
        goal={
            "topic": "阅读星空介绍",
            "source_event_ids": ["evt_test_1"],
            "scope": {"operation": "read_content", "url": URL},
        },
    )
    job = due(core)
    assert run(core.run_once(job_id=job))
    if gate == "pause":
        core.goals.pause_autonomy(True)
    elif gate == "budget":
        core.settings.exploration_daily_requests = 0
    elif gate == "adapter":
        core.settings.observation_enabled = False
    elif gate == "erasure":
        with core.store.transaction():
            core.ledger.expire_events(core.store, ["evt_test_1"], core.clock())
    else:
        core.settings.observation_adapters["bili"].source_grant_required = True
        assert core.exploration.schedule_goal()
        with pytest.raises(CoreError):
            core.reading.lease("bili")
        assert not core.store.rows("SELECT * FROM reading_attempts")
        assert_no_delivery(core)
        return
    assert not core.exploration.schedule_goal()
    assert not core.store.rows("SELECT * FROM exploration_requests")
    assert_no_delivery(core)


@pytest.mark.parametrize("outcome", ["FAILED", "UNKNOWN"])
def test_unsuccessful_read_does_not_invent_share_or_repeat_receipt(core, outcome):
    bug(core)
    configure(core)
    core.settings.life_loop.read_goals_enabled = True
    proposal(
        core,
        action="EXPLORE",
        destination="NONE",
        text="",
        goal={
            "topic": "阅读星空介绍",
            "source_event_ids": ["evt_test_1"],
            "scope": {"operation": "read_content", "url": URL},
        },
    )
    assert run(core.run_once(job_id=due(core)))
    assert core.exploration.schedule_goal()
    item = core.exploration.lease("bili")
    receipt = ReadResult(
        lease_token=item["lease_token"],
        status=outcome,
        error_code="READ_TIMEOUT",
        used_requests=1 if outcome == "FAILED" else None,
        used_bytes=20 if outcome == "FAILED" else None,
    )
    core.exploration.complete("bili", item["request_id"], receipt)
    assert core.exploration.complete("bili", item["request_id"], receipt)["duplicate"]
    assert not core.store.rows("SELECT * FROM observations")
    assert core.store.one("SELECT state FROM exploration_requests")["state"] == outcome
    core.clock.advance(3600)
    next_job = core.life_topics.schedule()
    if outcome == "UNKNOWN":
        assert next_job is None and len(core.model.calls) == 1
    else:
        # A known failure may be reviewed once; it is not a read result or retry.
        assert next_job
        proposal(core, action="SKIP", destination="NONE", text="")
        assert run(core.run_once(job_id=next_job))
        context = json.loads(core.model.calls[-1][0][-1]["content"])["decision_context"]
        assert context["execution_outcomes"][0]["state"] == "FAILED"
        assert context["execution_outcomes"][0]["requires_reconciliation"] is False
        assert len(core.model.calls) == 2
        core.clock.advance(3600)
        assert core.life_topics.schedule() is None
    assert not core.store.rows("SELECT * FROM observations")
    assert not core.store.rows("SELECT * FROM life_activity_results WHERE action='SHARE'")
    assert len(core.store.rows("SELECT * FROM exploration_requests")) == 1
    assert_no_delivery(core)


def test_native_goal_receipt_becomes_next_life_source(core):
    bug(core)
    configure(core)
    core.settings.life_loop.read_goals_enabled = True
    proposal(
        core,
        action="EXPLORE",
        destination="NONE",
        text="",
        goal={
            "topic": "阅读星空介绍",
            "source_event_ids": ["evt_test_1"],
            "scope": {"operation": "read_content", "url": URL},
        },
    )
    core.settings.observation_adapters["bili"].reader_max_requests = 4
    job = due(core)
    assert run(core.run_once(job_id=job))
    assert core.exploration.schedule_goal()

    async def read():
        pool, backend = mock_pool(response(META), response(PLAYER), response(SUBTITLE))
        async with (
            real_core_http(core) as port,
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                trust_env=False,
                headers={
                    "Authorization": "Bearer "
                    + core.settings.observation_adapters["bili"].token.get_secret_value()
                },
            ) as client,
            PublicHTTP(pool) as public,
        ):
            assert await tick(
                client,
                BilibiliReader(public, clock=core.clock),
                "bili",
                source="bilibili",
                clock=lambda: core.clock.now,
            )
            assert not await tick(
                client,
                BilibiliReader(public, clock=core.clock),
                "bili",
                source="bilibili",
                clock=lambda: core.clock.now,
            )
        assert len(backend.requests) == 3

    run(read())
    goal = core.store.one("SELECT * FROM goals")
    assert goal["state"] == "DONE"
    source_id = goal["result_event_id"]
    core.clock.advance(3600)
    core.model.activity = encode(
        {
            "action": "SHARE",
            "destination": "PRIVATE_MESSAGE_DRAFT",
            "source_event_ids": [source_id],
            "reason": "实际阅读结果",
            "text": "恒星颜色与温度有关。",
        }
    )
    next_job = core.life_topics.schedule()
    assert next_job
    assert run(core.run_once(job_id=next_job))
    result = core.store.one("SELECT * FROM life_activity_results WHERE job_id=?", (next_job,))
    assert result["state"] == "SHADOW", core.store.rows("SELECT * FROM jobs")
    assert result["action"] == "SHARE"
    assert_no_delivery(core)
