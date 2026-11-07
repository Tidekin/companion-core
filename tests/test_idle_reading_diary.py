"""A quiet day through native scheduling, loopback Reader HTTP, judgement and Diary."""

import json
from datetime import timedelta

import httpx
import pytest

from companion.autonomous_selector import native_selector
from companion.core import Core
from companion.life_protocol import LifeLoopConfig, LifeShadowApproval
from companion.model import ModelClient
from companion.store import encode
from source_reader.bilibili import BilibiliReader
from source_reader.network import PublicHTTP
from source_reader.worker_v1 import tick
from tests.conftest import ingest, run
from tests.support.core_http import real_core_http
from tests.support.exploration import configure
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare
from tests.support.source_reader import META, PLAYER, SUBTITLE, response
from tests.test_heybox_reader import mock_pool
from tests.test_topic_selection import judged


def enable(core):
    configure(core)
    cfg = core.settings
    cfg.wake_enabled = cfg.content_enabled = cfg.diary_enabled = True
    cfg.watch_ledger.enabled = True
    cfg.life_loop = LifeLoopConfig(mode="SHADOW", read_goals_enabled=True, autonomy_enabled=True)
    cfg.quiet_start = cfg.quiet_end = 0
    cfg.observation_adapters["bili"].reader_max_requests = 4
    cfg.exploration_daily_requests = 20


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
                assert (await lanes["wake"].run()).progressed
                request = core.store.one("SELECT * FROM exploration_requests")
                assert (request["adapter_id"], request["query"]) == ("bili", "恒星")
                assert not (await lanes["wake"].run()).progressed
                # Ten earlier titles would previously crowd the substantive read out of Diary.
                listing = [{"type": "video", "title": "恒星图谱", "bvid": f"BV1{x:08d}a"}
                           for x in range(12)]
                pool, backend = mock_pool(response({"code": 0, "data": {"result": listing}}),
                                          response(META), response(PLAYER), response(SUBTITLE))
                auth = {"Authorization": "Bearer " + cfg.observation_adapters["bili"]
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
                    core.clock.advance(cfg.wake_min_interval_minutes * 60 + 1)
                    assert (await lanes["wake"].run()).progressed
                    assert (await lanes["goal"].run()).progressed
                    assert await tick(client, reader, "bili", source="bilibili",
                                      clock=lambda: core.clock.now)
                assert len(backend.requests) == 4
                assert core.store.one("SELECT 1 FROM goals WHERE state='DONE'")
                assert (await lanes["watch"].run()).progressed
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
        with core.store.transaction():
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


def test_open_topic_has_priority_and_pause_and_daily_limit_still_apply(core):
    from tests.test_life_topics import bug

    topic = bug(core)
    enable(core)
    core.clock.advance(core.settings.proactive_idle_seconds + 1)
    assert topic and not core.wake.evaluate()
    core.store.execute("UPDATE life_topics SET state='CLOSED'")
    core.store.execute("INSERT OR REPLACE INTO runtime_meta VALUES ('life_paused','1')")
    assert not core.wake.evaluate()
    core.store.execute("DELETE FROM runtime_meta WHERE key='life_paused'")
    core.clock.advance(core.settings.wake_min_interval_minutes * 60 + 1)
    core.settings.wake_max_daily_runs = 0
    assert not core.wake.evaluate()
    assert core.store.one("SELECT reason_code FROM wake_decisions ORDER BY rowid DESC LIMIT 1") == {
        "reason_code": "DAILY_LIMIT"}
