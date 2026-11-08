"""Public discovery and deferred reflection admission recover without replaying paid I/O."""

import asyncio
import json
import ssl
from datetime import date, datetime

import httpcore
import httpx
import pytest

from companion.core import Core
from source_reader.bilibili import BilibiliReader
from source_reader.network import PublicHTTP
from source_reader.worker import tick
from tests.conftest import ingest, run
from tests.support.consolidation import enable_nightly
from tests.support.source_reader import ITEM, META, Responses, read, response


def test_bilibili_discovery_reads_the_bounded_public_feed_once():
    rows = [{"bvid": "BV1xx411c7mD", "title": "公开视频", "uri": "https://untrusted.invalid/"},
            {"bvid": "BV1xx411c7mD", "title": "重复视频"},
            {"bvid": "BV2xx411c7mD", "title": "另一个视频"}]
    result, backend = run(read(response({"code": 0, "data": {"list": rows}}),
                               item={**ITEM, "operation": "discover", "url": "", "max_items": 1}))
    assert result["status"] == "SUCCESS" and result["used_requests"] == 1
    assert result["items"] == [{"url": "https://www.bilibili.com/video/BV1xx411c7mD",
                                "title": "公开视频", "text": ""}]
    assert backend.requests[0].startswith(b"GET /x/web-interface/popular?pn=1&ps=1 ")
    assert backend.server_names == ["api.bilibili.com"]


@pytest.mark.parametrize("status,code", [(302, "UPSTREAM_HTTP_ERROR"),
                                        (403, "UPSTREAM_ACCESS_LIMIT"),
                                        (412, "UPSTREAM_ACCESS_LIMIT")])
def test_discovery_access_failure_does_not_switch_targets(status, code):
    result, backend = run(read(response(status=status),
                               item={**ITEM, "operation": "discover", "url": ""}))
    assert result["status"] == "FAILED" and result["error_code"] == code
    assert len(backend.requests) == 1 and backend.server_names == ["api.bilibili.com"]


def seed_closed_days(core):
    model = enable_nightly(core, seed=False)
    core.settings.diary_enabled = True
    for number, day in enumerate((date(2026, 10, 16), date(2026, 10, 17)), start=1):
        for index in range(3):
            ingest(core, number * 10 + index, "这一天我们聊了读书安排。",
                   at=day.isoformat() + "T04:00:00+00:00")
    core.store.execute("UPDATE jobs SET state='SUCCEEDED'")
    return model


def test_reflection_daily_admission_wait_resumes_after_midnight_and_restart(core):
    model = seed_closed_days(core)
    first, second = date(2026, 10, 16), date(2026, 10, 17)
    assert run(core.reflections.run_day(first))["state"] == "COMMITTED"
    waiting = run(core.reflections.run_day(second))
    assert waiting["state"] == "RETRY_WAIT"
    assert waiting["last_error_code"] == "SCENARIO_CALL_LIMIT"
    usage = core.store.rows("SELECT * FROM model_usage")
    assert len(usage) == 1 and usage[0]["state"] == "SETTLED"
    assert run(core.reflections.run_day(second)) == waiting
    core.clock.now = datetime.fromisoformat(waiting["next_attempt_at"])
    with Core(core.settings, model, core.clock) as restarted:
        assert run(restarted.reflections.run_day(second))["state"] == "COMMITTED"
        assert run(restarted.reflections.run_day(first))["state"] == "COMMITTED"
    assert len(core.store.rows("SELECT * FROM model_usage")) == 2
    assert len([scenario for _, scenario in model.calls if scenario == "reflection"]) == 2


def test_nightly_recovers_legacy_unadmitted_reflection_without_regenerating_diary(core):
    model = seed_closed_days(core)
    first, second = date(2026, 10, 16), date(2026, 10, 17)
    for day in (first, second):
        core.consolidation.schedule_day(day)
        assert run(core.consolidation.run_once())
    assert core.consolidation.status(first)["state"] == "COMPLETE"
    assert core.consolidation.status(second)["state"] == "PARTIAL"
    # A previous release recorded an unadmitted quota failure, then a clean window STOP.
    core.store.execute((
        "UPDATE reflection_runs SET state='FAILED',next_attempt_at=NULL WHERE"
        ' local_date=%s'
    ), (second.isoformat(),))
    core.store.execute((
        'UPDATE consolidation_runs SET '
        "state='CANCELLED',last_error_code='LIVE_WINDOW_STOPPED' WHERE "
        'local_date=%s'
    ),
                       (second.isoformat(),))
    before = list(model.calls)
    core.settings.reflection_max_daily_calls = 3
    core.consolidation.schedule_day(second)
    assert run(core.consolidation.run_once())
    assert core.consolidation.status(second)["state"] == "COMPLETE"
    assert [scenario for _, scenario in model.calls[len(before):]] == ["reflection"]
    assert len(core.store.rows("SELECT * FROM diary_runs WHERE state='COMMITTED'")) == 2


@pytest.mark.parametrize("change", ["source", "erasure"])
def test_waiting_reflection_never_reuses_changed_or_erased_sources(core, change):
    model = seed_closed_days(core)
    first, second = date(2026, 10, 16), date(2026, 10, 17)
    assert run(core.reflections.run_day(first))["state"] == "COMMITTED"
    waiting = run(core.reflections.run_day(second))
    assert waiting["state"] == "RETRY_WAIT"
    if change == "erasure":
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, ["evt_test_20"], core.clock())
    else:
        core.store.execute("UPDATE events SET body_json=%s WHERE event_id='evt_test_20'",
                           ('{"text":"来源发生变化","media":[]}',))
    core.clock.now = datetime.fromisoformat(waiting["next_attempt_at"])
    assert run(core.reflections.run_day(second))["state"] == "CANCELLED"
    assert len(model.calls) == 1
    assert len(core.store.rows("SELECT * FROM model_usage")) == 1


@pytest.mark.parametrize("code", ["MODEL_RETRYABLE", "SCENARIO_CALL_LIMIT",
                                 "MODEL_PROVIDER_CONCURRENCY_EXHAUSTED"])
def test_unknown_reflection_is_not_replayed_as_an_admission_retry(core, code):
    from companion.contracts import CoreError

    model = seed_closed_days(core)
    day = date(2026, 10, 16)
    model.reflection_error = CoreError(code, 503)
    failed = run(core.reflections.run_day(day))
    assert failed["state"] == "FAILED"
    core.clock.advance(86400)
    core.settings.reflection_max_daily_calls = 4
    model.reflection_error = None
    assert run(core.reflections.run_day(day)) == failed
    assert len(model.calls) == 1
    assert core.store.one("SELECT state FROM model_usage")["state"] == "UNKNOWN"


def test_stop_cancels_an_unadmitted_reflection_wait(core):
    from companion import private_life

    seed_closed_days(core)
    run(core.reflections.run_day(date(2026, 10, 16)))
    waiting = run(core.reflections.run_day(date(2026, 10, 17)))
    assert waiting["state"] == "RETRY_WAIT"
    with core.store.transaction():
        private_life.stop_work(core.store, core.clock())
    row = core.store.one('SELECT * FROM reflection_runs WHERE run_id=%s', (waiting["run_id"],))
    assert row["state"] == "CANCELLED" and row["last_error_code"] == "LIVE_WINDOW_STOPPED"
    assert row["model_request_id"] is None


@pytest.mark.parametrize("payload", [{"code": 0, "data": {}},
                                     {"code": 0, "data": {"list": ["invalid"]}},
                                     {"code": -101}])
def test_discovery_invalid_feed_never_falls_back_to_a_page(payload):
    result, backend = run(read(response(payload),
                               item={**ITEM, "operation": "discover", "url": ""}))
    assert result["status"] == "FAILED" and not result["items"]
    assert len(backend.requests) == 1


def test_real_api_and_independent_worker_recover_backlog_and_public_exploration(
    core, tmp_path, monkeypatch,
):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from companion import api, life_worker
    from companion.config import ObservationAdapterConfig
    from companion.diary import DIARY_RULES
    from companion.model import ModelClient
    from companion.reflection import REFLECTION_RULES
    from tests.native_continuous_support import host
    from tests.support.consolidation import ReflectionModel
    from tests.support.native_context import Capture
    from tests.support.onebot_events import raw_event

    fixture_model = ReflectionModel()
    calls, instances = [], []
    number = 0

    async def source(ws):
        nonlocal number
        number += 1
        await ws.send(json.dumps(raw_event(number, text="这一天我们聊了读书安排。",
                                          time=int(core.clock.now.timestamp()))))
        await ws.wait_closed()

    def construct(*args, **kwargs):
        instance = Core(*args, clock=core.clock, **kwargs)
        instances.append(instance)
        return instance

    async def respond(request):
        messages = json.loads(request.content)["messages"]
        rules = messages[0]["content"]
        calls.append(rules)
        scenario = "diary" if rules == DIARY_RULES else (
            "reflection" if rules == REFLECTION_RULES else "chat")
        result = await fixture_model.complete(messages, scenario)
        return httpx.Response(200, json={"choices": [{"message": {"content": result.text}}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 40}})

    async def scenario():
        options = dict(diary_enabled=True, consolidation_enabled=True, reflection_enabled=True,
            goals_enabled=True, exploration_enabled=True, reflection_max_daily_calls=1,
            reflection_min_sources=1, reflection_daily_micro_usd=50000,
            reflection_monthly_micro_usd=100000, extraction_min_events=40,
            wake_max_daily_runs=0, content_enabled=True, observation_enabled=True,
            exploration_daily_requests=8, exploration_monthly_requests=30,
            exploration_daily_bytes=4_194_304,
            observation_adapters={"bili": ObservationAdapterConfig(
                source="bilibili", token="synthetic-recovery-reader-token")})
        monkeypatch.setattr(api, "Core", construct)
        monkeypatch.setattr(life_worker, "Core", construct)
        async with host(core, tmp_path, source, ticking=False, independent_api=True,
            model_reply=respond, core_options=options,
            purposes=("chat", "history", "memory", "diary", "consolidation", "reflection",
                      "goals", "content", "exploration"),
        ) as (bridge, _, sends, _):
            core.diary.model = core.reflections.model = core.model
            start = core.clock.now.astimezone(ZoneInfo(core.settings.timezone))
            first, second = start.date(), (start + timedelta(days=1)).date()
            receiver = Capture(core, bridge.spool, bridge.runtime)
            for offset in (0, 1):
                core.clock.now = start + timedelta(days=offset)
                for _ in range(3):
                    await receiver.capture_once(bridge.client, bridge.protocol)
                    await bridge.flush()
                assert await core.run_once()
                assert await bridge.pump()
            core.clock.now = (start + timedelta(days=2)).replace(hour=4)
            for day in (first, second):
                core.consolidation.schedule_day(day)
                assert await core.consolidation.run_once()
            assert core.consolidation.status(first)["state"] == "COMPLETE"
            assert core.consolidation.status(second)["state"] == "PARTIAL"
            pending = core.store.one('SELECT * FROM reflection_runs WHERE local_date=%s',
                                     (second.isoformat(),))
            assert pending["state"] == "RETRY_WAIT" and pending["model_request_id"] is None
            core.store.execute((
                'UPDATE reflection_runs SET '
                "state='FAILED',next_attempt_at=NULL WHERE local_date=%s"
            ), (second.isoformat(),))
            core.store.execute((
                'UPDATE consolidation_runs SET '
                "state='CANCELLED',last_error_code='LIVE_WINDOW_STOPPED' "
                'WHERE local_date=%s'
            ), (second.isoformat(),))
            diaries = core.store.rows("SELECT * FROM diary_runs ORDER BY local_date")
            existing_usage = core.store.rows("SELECT * FROM model_usage")
            baseline_sends = len(sends)
            calls.clear()
            core.settings.reflection_max_daily_calls = 4
            await receiver.capture_once(bridge.client, bridge.protocol)
            await bridge.flush()
            assert await core.run_once()
            assert await bridge.pump()
            from companion.autonomous_selector import foreground_clear

            assert foreground_clear(core), core.store.rows(
                "SELECT kind,state,last_error_code FROM jobs")
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as model:
                monkeypatch.setattr(life_worker, "ModelClient",
                                    lambda settings, _: ModelClient(settings, model))
                task = asyncio.create_task(life_worker.life_worker(core.settings))
                try:
                    async with asyncio.timeout(12):
                        while core.consolidation.status(second)["state"] != "COMPLETE":
                            if task.done():
                                task.result()
                            await asyncio.sleep(0.02)
                except TimeoutError:
                    raise AssertionError({"nightly": core.consolidation.status(second),
                        "reflection": core.store.rows("SELECT * FROM reflection_runs"),
                        "calls": calls}) from None
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            assert len(instances) == 2 and all(instance is not core for instance in instances)
            assert calls.count(REFLECTION_RULES) == 1
            assert core.store.rows((
                'SELECT * FROM diary_runs WHERE local_date IN (%s,%s) ORDER '
                'BY local_date'
            ), (first.isoformat(), second.isoformat())) == diaries
            for row in existing_usage:
                assert core.store.one('SELECT * FROM model_usage WHERE request_id=%s',
                                      (row["request_id"],)) == row
            assert len(sends) == baseline_sends + 1
            with core.store.transaction():
                core.exploration.create("bili", "discover", "synthetic-repaired-discovery")
            backend = Responses(
                response({"code": 0, "data": {"list": [{"bvid": "BV1xx411c7mD",
                                                        "title": "公开探索标题"}]}}),
                response(META))
            pool = httpcore.AsyncConnectionPool(
                ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
                network_backend=backend, max_keepalive_connections=0)
            bridge.client.headers["Authorization"] = "Bearer synthetic-recovery-reader-token"
            async with PublicHTTP(pool) as public:
                reader = BilibiliReader(public, clock=core.clock, subtitles=False)
                assert await tick(bridge.client, reader, "bili")
                goal = core.store.one("SELECT * FROM goals WHERE topic='公开探索标题'")
                assert goal
                core.goals.accept(goal["goal_id"])
                assert core.exploration.schedule_goal()
                assert await tick(bridge.client, reader, "bili")
                assert not await tick(bridge.client, reader, "bili")
            assert core.goals.get(goal["goal_id"])["state"] == "DONE"
            assert core.store.rows(
                'SELECT result_status FROM observations ORDER BY '
                'record_sequence'
            ) == [
                {"result_status": "DISCOVERED"}, {"result_status": "FETCHED"}]
            assert len(backend.requests) == 2
            assert backend.server_names == ["api.bilibili.com"] * 2
            assert all(row["state"] == "SETTLED"
                       for row in core.store.rows("SELECT state FROM model_usage"))
    run(scenario())
