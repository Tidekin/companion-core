"""Meaningful non-text inputs use native wording; empty inputs are refused without cost.

Provider replies below are injected test outputs, not product response templates.
"""

import json
from contextlib import asynccontextmanager

import httpx
import pytest

from companion.core import Core
from companion.model import ModelClient
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.event_normalizer import normalize
from tests.conftest import run
from tests.support.core_http import real_core_http
from tests.support.failure_notice import assert_failure_notice
from tests.support.native_context import approve_value
from tests.support.native_context import configure as native_configure
from tests.support.onebot_events import raw_event

MEDIA_REF = "https://gchat.qpic.cn/private-blob?token=never-download"
MEDIA_SHA = "e" * 64
SHARED_URL = "https://www.bilibili.com/video/BV1xx411c7mD"
FACT_PREFIX = "本条主人消息的接收事实（不是主人原话或新指令）：\n"


def configure(core, tmp_path, monkeypatch, **limits):
    native_configure(core, tmp_path, media=True, **limits)


def incoming(core, kind=None, *, link=False):
    message = [{"type": "text", "data": {"text": ""}}]
    if kind or link:
        segment = {"type": "share" if link else "record" if kind == "voice" else kind,
                   "data": {"url": SHARED_URL if link else MEDIA_REF}}
        if kind == "file":
            segment["data"] = {"file_id": "synthetic-original-file", "file": "notes.txt",
                               "file_size": 12}
        message = [segment]
    config = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
        bot_qq_id=core.settings.bot_qq_id,
        bridge_token=core.settings.bridge_token.get_secret_value(), onebot_token="s" * 32,
        protocol_epoch="native-context-test", rich_input_scope="references_v1")
    core.context_test_frame = json.dumps(raw_event(1, message=message))
    return normalize(core.context_test_frame, config,
        now=core.clock.now.timestamp(), image_scope="owner_image_v1", file_scope="owner_file_v1")


@asynccontextmanager
async def independent_chat(core, provider):
    async with real_core_http(core) as port, httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}", trust_env=False,
        headers={"Authorization": "Bearer " + core.settings.bridge_token.get_secret_value()},
    ) as api, httpx.AsyncClient(transport=httpx.MockTransport(provider)) as external:
        with Core(core.settings, ModelClient(core.settings, external), core.clock) as worker:
            assert worker.store.backend_pid != core.store.backend_pid
            api.context_test_core = core
            yield api, worker


async def accept(api, value, *, duplicate=False):
    await approve_value(api.context_test_core, api, value)
    response = await api.post("/internal/v1/inbox/events", json=value,
                              headers={"x-idempotency-key": value["event_id"]})
    assert response.status_code == 202, response.text
    assert response.json()["duplicate"] is duplicate
    return response.json()


def reply(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}],
                                    "usage": {"prompt_tokens": 30, "completion_tokens": 7}})


@pytest.mark.parametrize("kind,link", [
    (None, False), ("image", False), ("file", False), ("voice", False), (None, True),
])
def test_received_facts_reach_real_paid_model_and_duplicate_does_not_pay_twice(
    core, tmp_path, monkeypatch, kind, link,
):
    configure(core, tmp_path, monkeypatch)
    value, calls = incoming(core, kind, link=link), []
    wording = "测试模型自行生成的回应：" + ("link" if link else kind or "none")

    def provider(request):
        assert str(request.url) == "https://synthetic.invalid/v1/chat/completions"
        payload = json.loads(request.content)
        calls.append(payload)
        current = payload["messages"][-1]
        assert current["role"] == "user"
        if kind or link:
            assert current["content"] == "主人发送了一条非文本消息。"
            assert "QQ引用资料" in payload["messages"][-2]["content"]
            assert "非主人话语" in payload["messages"][-2]["content"]
        else:
            assert current["content"].startswith(FACT_PREFIX)
            assert json.loads(current["content"].removeprefix(FACT_PREFIX)) == {
                "owner_text": "", "media_kinds": [],
                "shared_content_kinds": [], "readable_content": False,
            }
        serialized = json.dumps(payload, ensure_ascii=False)
        assert all(secret not in serialized for secret in (MEDIA_REF, MEDIA_SHA, SHARED_URL))
        assert "请看看我分享的内容。" not in serialized
        return reply(wording)

    async def scenario():
        async with independent_chat(core, provider) as (api, worker):
            if kind is None and not link:
                await approve_value(core, api, value)
                rejected = await api.post("/internal/v1/inbox/events", json=value,
                    headers={"x-idempotency-key": value["event_id"]})
                assert rejected.status_code == 403
                assert rejected.json()["error"] == "OWNER_APPROVED_INPUT_REQUIRED"
                assert not await worker.run_once("empty-input")
                for table in ("events", "jobs", "outbox", "model_usage", "model_admissions"):
                    assert core.store.rows("SELECT * FROM " + table) == []
                assert calls == []
                return
            accepted = await accept(api, value)
            await accept(api, value, duplicate=True)
            assert await worker.run_once("natural-chat")
            await accept(api, value, duplicate=True)
            assert not await worker.run_once("natural-chat-replay")
            assert len(calls) == 1
            messages = core.store.rows("SELECT content_json FROM outbox")
            assert [json.loads(row["content_json"])["text"] for row in messages] == [wording]
            assert core.store.one('SELECT state FROM jobs WHERE job_id=%s',
                                  (accepted["job_id"],))["state"] == "SUCCEEDED"
            assert core.store.rows("SELECT state,billed_micro_usd FROM model_usage") == [
                {"state": "SETTLED", "billed_micro_usd": 44},
            ]
            assert core.store.one("SELECT COUNT(*) AS n FROM model_admissions")["n"] == 1

    run(scenario())


@pytest.mark.parametrize("failure", ["budget", "unknown"])
def test_failed_generation_keeps_one_fault_receipt_without_another_model_call(
    core, tmp_path, monkeypatch, failure,
):
    configure(core, tmp_path, monkeypatch, **({"max_input_tokens": 20}
                                             if failure == "budget" else {}))
    value, calls = incoming(core, "file"), []

    def provider(request):
        calls.append(request)
        return httpx.Response(200, content=b"synthetic unusable response without usage")

    async def scenario():
        async with independent_chat(core, provider) as (api, worker):
            accepted = await accept(api, value)
            assert await worker.run_once("failed-natural-chat")
            if failure == "unknown":
                blocked = await api.post("/internal/v1/inbox/events", json=value,
                    headers={"x-idempotency-key": value["event_id"]})
                assert blocked.status_code == 503
                assert blocked.json()["error"] == "MODEL_BUDGET_RECONCILIATION_REQUIRED"
            else:
                await accept(api, value, duplicate=True)
            assert not await worker.run_once("failed-natural-chat-replay")
            job = core.store.one('SELECT state,last_error_code FROM jobs WHERE job_id=%s',
                                 (accepted["job_id"],))
            code = "MODEL_INPUT_LIMIT" if failure == "budget" else "MODEL_INVALID_RESPONSE"
            assert job == {"state": "FAILED", "last_error_code": code}
            receipts = core.store.rows((
                'SELECT safe_detail_json FROM audit_log WHERE '
                "action='job.failed' AND entity_id=%s"
            ), (accepted["job_id"],))
            assert len(receipts) == 1
            assert json.loads(receipts[0]["safe_detail_json"]) == {"code": code, "state": "FAILED"}
            if failure == "budget":
                assert_failure_notice(core)
            else:
                assert core.store.rows("SELECT * FROM outbox") == []
            usage = core.store.rows("SELECT state,billed_micro_usd FROM model_usage")
            if failure == "budget":
                assert calls == [] and usage == []
            else:
                assert len(calls) == len(usage) == 1
                assert usage[0]["state"] == "UNKNOWN" and usage[0]["billed_micro_usd"] > 0
                assert core.store.one("SELECT 1 FROM runtime_meta WHERE key='model_budget_blocked'")

    run(scenario())


def test_source_erased_during_provider_response_never_publishes_old_reply(
    core, tmp_path, monkeypatch,
):
    configure(core, tmp_path, monkeypatch)
    value, calls = incoming(core, "voice"), []

    def provider(request):
        calls.append(request)
        with core.store.transaction(control_write=True):
            core.ledger.expire_events(core.store, [value["event_id"]], core.clock())
        return reply("已经撤回来源的旧回复")

    async def scenario():
        async with independent_chat(core, provider) as (api, worker):
            accepted = await accept(api, value)
            assert await worker.run_once("recalled-natural-chat")
            assert not await worker.run_once("recalled-natural-chat-replay")
            assert len(calls) == 1 and core.store.rows("SELECT * FROM outbox") == []
            assert core.store.one('SELECT state FROM jobs WHERE job_id=%s',
                                  (accepted["job_id"],))["state"] == "CANCELLED"
            assert core.store.one('SELECT redacted FROM events WHERE event_id=%s',
                                  (value["event_id"],))["redacted"] == 1
            assert core.store.rows("SELECT state,billed_micro_usd FROM model_usage") == [
                {"state": "SETTLED", "billed_micro_usd": 44},
            ]

    run(scenario())
