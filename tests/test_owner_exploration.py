"""Natural owner browsing requests reach a real Reader queue, never an invented tool."""

import json
import ssl

import httpcore
import httpx
import pytest

from companion.core import Core
from companion.model import ModelClient
from companion.store import encode
from source_reader import worker_v1
from source_reader.bilibili import BilibiliReader
from source_reader.network import PublicHTTP
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, call, worker_for
from tests.support.reading_followup import READER_AUTH, admit, window
from tests.support.source_reader import META, Responses, response


@pytest.mark.parametrize("text", [
    "你现在应该可以刷b站了吧", "去看看小黑盒吧", "请浏览 bilibili", "刷B站",
    "不要刷B站", "他说‘刷B站’", "我昨天刷B站", "刷B站后给别人发消息",
    "我想知道为什么他说不能刷B站", "你还是不能刷b站吗",
])
def test_ingress_does_not_select_tools_by_phrase(core, tmp_path, monkeypatch, text):
    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, config, plan):
            await admit(client, core, config, plan, 1, text)
            assert not core.store.one("SELECT 1 FROM exploration_requests")
    run(scenario())


async def select_discovery(core):
    provider = Provider(answer(calls=(
        call("source_discovery", "discover_content", {"source": "bilibili"}),
    )), answer("根据原状态说明当前可用性。"))
    async with worker_for(core, provider) as worker:
        assert await worker.run_once("model-selected-discovery")
    return provider


@pytest.mark.parametrize("context_soft_tokens", [3200, 16000])
def test_signed_owner_request_runs_one_discovery_and_reports_its_real_coverage(
    core, tmp_path, monkeypatch, context_soft_tokens,
):
    async def scenario():
        async with window(core, tmp_path, monkeypatch,
                          context_soft_tokens=context_soft_tokens) as (client, config, plan):
            event, accepted = await admit(client, core, config, plan, 1, "你现在可以刷B站了吧")
            await select_discovery(core)
            queued = core.store.one("SELECT * FROM exploration_requests")
            assert queued["operation"] == "discover" and queued["state"] == "QUEUED"
            assert queued["request_key"] == "chat-tool:" + accepted["job_id"] + ":source_discovery"
            assert core.store.rows("SELECT event_id FROM exploration_evidence") == [
                {"event_id": event["event_id"]},
            ]
            assert core.claim_job("not-until-source-result") is None
            backend = Responses(response({"code": 0, "data": {"list": [
                {**META["data"], "bvid": "BV1xx411c7mD", "title": "发现的恒星故事"},
            ]}}))
            pool = httpcore.AsyncConnectionPool(
                ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
                network_backend=backend, max_keepalive_connections=0,
            )
            calls = []

            def provider(request):
                payload = json.loads(request.content)
                calls.append(payload)
                assert any(tool["function"]["name"] == "discover_content"
                           for tool in payload["tools"])
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": "发现了一个恒星故事的标题，还没读正文。"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                })

            async with PublicHTTP(pool) as public, httpx.AsyncClient(
                base_url=str(client.base_url), headers=READER_AUTH, trust_env=False,
            ) as reader_http, httpx.AsyncClient(
                transport=httpx.MockTransport(provider),
            ) as model_http:
                reader = BilibiliReader(public, clock=core.clock)
                assert await worker_v1.tick(reader_http, reader, "bili", source="bilibili",
                                            clock=lambda: core.clock.now)
                assert not await worker_v1.tick(reader_http, reader, "bili", source="bilibili",
                                                clock=lambda: core.clock.now)
                with Core(core.settings, ModelClient(core.settings, model_http),
                          core.clock) as independent:
                    assert await independent.run_once("browsing-reply")
                    assert not await independent.run_once("no-repeat")
            assert len(backend.requests) == 1 and not backend.rows
            assert len(calls) == 1
            prompt = encode(calls[0]["messages"])
            assert "发现的恒星故事" in prompt and "DISCOVERED" in prompt
            assert "不是视频内容" in prompt and "系统未启用外部执行工具" not in prompt
            assert core.store.one("SELECT state FROM exploration_requests")["state"] == "DONE"
            assert core.store.one('SELECT state FROM jobs WHERE job_id=%s', (accepted["job_id"],))[
                "state"] == "SUCCEEDED"
            sources = core.store.rows('SELECT event_id FROM job_sources WHERE job_id=%s',
                                      (accepted["job_id"],))
            assert {row["event_id"] for row in sources} >= {
                event["event_id"], core.store.one("SELECT event_id FROM observations")["event_id"],
            }
    run(scenario())


def test_paused_life_does_not_schedule_natural_browsing(core, tmp_path, monkeypatch):
    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, config, plan):
            core.store.execute(
                'INSERT INTO runtime_meta (key,value) VALUES '
                "('life_paused','1') ON CONFLICT(key) DO UPDATE SET "
                'value=excluded.value,record_sequence=DEFAULT'
            )
            await admit(client, core, config, plan, 1, "刷B站")
            await select_discovery(core)
            assert not core.store.rows("SELECT * FROM exploration_requests")
    run(scenario())


@pytest.mark.parametrize("context_soft_tokens", [3200, 16000])
def test_capability_question_uses_current_system_facts_without_requesting_another_read(
    core, tmp_path, monkeypatch, context_soft_tokens,
):
    legacy_reply = "我这边没有能打开 B 站页面的通道。"
    calls = []

    def provider(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": legacy_reply if len(calls) == 1
                                     else "当前可安排 B站读取，具体结果以读取回执为准。"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    async def scenario():
        async with window(core, tmp_path, monkeypatch,
                          context_soft_tokens=context_soft_tokens) as (client, config, plan):
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model_http:
                with Core(core.settings, ModelClient(core.settings, model_http),
                          core.clock) as first_worker:
                    first, _ = await admit(client, core, config, plan, 1, "之前有读取通道吗")
                    assert await first_worker.run_once("legacy-reply-fixture")
                # Retain an already delivered historical refusal, as in the reported turn.
                core.store.execute((
                    "UPDATE outbox SET state='SENT',sent_at=%s WHERE "
                    'origin_event_id=%s'
                ), (core.clock(), first["event_id"]))
                latest, accepted = await admit(client, core, config, plan, 2, "你还是不能刷b站吗")
                assert not core.store.rows("SELECT * FROM exploration_requests")
                assert not core.store.rows("SELECT * FROM content_items")
                with Core(core.settings, ModelClient(core.settings, model_http),
                          core.clock) as restarted_worker:
                    assert await restarted_worker.run_once("capability-question")
                    assert not await restarted_worker.run_once("no-replay")
            assert len(calls) == 2
            assert all(any(tool["function"]["name"] == "read_content"
                           for tool in payload["tools"]) for payload in calls)
            messages = calls[1]["messages"]
            rules = messages[0]["content"]
            assert messages[0]["role"] == "system"
            assert "以本轮状态为准；历史中的能力自述可能已过时" in rules
            assert "系统流程和主人指令也是已接入的能力" in rules
            assert "本轮没有可直接调用的函数，不表示这些能力未接入" in rules
            assert "单次读取失败只说明该次失败" in rules
            if context_soft_tokens == 16000:
                assert "reading.bili：可用（系统流程/主人指令）" in rules
                assert {"role": "assistant", "content": legacy_reply} in messages
            else:
                assert "reading.bili" in rules.split("可用：", 1)[1].split("。", 1)[0]
            assert messages[-1] == {"role": "user", "content": "你还是不能刷b站吗"}
            assert core.store.one('SELECT state FROM jobs WHERE job_id=%s',
                                  (accepted["job_id"],))["state"] == "SUCCEEDED"
            assert not core.store.rows("SELECT * FROM exploration_requests")
            assert not core.store.rows("SELECT * FROM reading_attempts")
            assert core.store.rows("SELECT scenario,state FROM model_usage") == [
                {"scenario": "chat", "state": "SETTLED"},
                {"scenario": "chat", "state": "SETTLED"},
            ]
            assert core.store.one('SELECT content_json FROM outbox WHERE origin_event_id=%s',
                                  (latest["event_id"],))
    run(scenario())


def test_uncertain_browsing_keeps_the_original_attempt_and_expense(core, tmp_path, monkeypatch):
    async def scenario():
        async with window(core, tmp_path, monkeypatch) as (client, config, plan):
            event, accepted = await admit(client, core, config, plan, 1, "刷B站")
            await select_discovery(core)
            row = core.store.one("SELECT * FROM exploration_requests")
            core.store.execute((
                'UPDATE exploration_requests SET '
                "state='UNKNOWN',last_error_code='READER_NETWORK_UNKNOWN' "
                'WHERE request_id=%s'
            ),
                               (row["request_id"],))
            await admit(client, core, config, plan, 1, "刷B站")
            assert len(core.store.rows("SELECT * FROM exploration_requests")) == 1
            assert core.store.one("SELECT state FROM exploration_requests")["state"] == "UNKNOWN"
            saved = core.store.one("SELECT request_key FROM exploration_requests")
            assert saved["request_key"] == "chat-tool:" + accepted["job_id"] + ":source_discovery"
    run(scenario())
