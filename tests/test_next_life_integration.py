"""Actual owner HTTP routes and the native independent Life worker, with mock model I/O."""

import asyncio
import json
from datetime import UTC, timedelta
from functools import partial
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from companion import life_worker, owner_authority
from companion.api import create_app
from companion.core import Core
from companion.diary import DIARY_RULES, DiaryService
from companion.model import ModelClient
from companion_onebot_bridge import owner_notices
from tests.conftest import run
from tests.native_continuous_support import host
from tests.support.diary import DAY
from tests.support.native_context import Capture
from tests.support.next_life_diary import seed, withdraw
from tests.support.onebot_events import raw_event
from tests.support.owner_recall_bridge import recall_frame
from tests.support.private_companion import companion_options

ADMIN_TOKEN = "synthetic-diary-owner-token-32-characters"


@pytest.mark.parametrize("state,label", [
    ("PENDING", "已排队，等待整理"),
    ("RUNNING", "正在整理"),
    ("RETRY_WAIT", "正在等待重试"),
    ("NOT_SCHEDULED", "没有安排日记重算"),
])
def test_command_and_owner_api_report_actual_queue_state(core, state, label):
    model = seed(core)
    run_id = None
    if state != "NOT_SCHEDULED":
        withdraw(core)
        run_id = core.diary.recompute_day(DAY, core.settings.owner_qq_id)
        if state in {"RUNNING", "RETRY_WAIT"}:
            claimed = core.diary.repo.claim("state-fixture")
            if state == "RETRY_WAIT":
                core.diary.repo.finish(claimed, "RETRY_WAIT", "MODEL_RETRYABLE")
    core.settings.diary_admin_token = SecretStr(ADMIN_TOKEN)
    app = create_app(core.settings)
    app.state.core = core
    client = TestClient(app)
    url = f"/internal/v1/diary/{DAY}/recompute"
    assert client.post(url).status_code == 401
    assert client.post(url, headers={
        "Authorization": "Bearer " + core.settings.bridge_token.get_secret_value(),
    }).status_code == 401
    headers = {"Authorization": "Bearer " + ADMIN_TOKEN}
    for _ in range(2):
        response = client.post(url, headers=headers)
        assert response.status_code == 202
        assert response.json() == {
            "local_date": DAY.isoformat(), "run_id": run_id, "state": state,
        }
        reply = core.diary.command(f"/重算日记 {DAY}", core.settings.owner_qq_id)
        assert label in reply and "已保存" not in reply
    assert len(model.calls) == 1  # Both entry points only schedule; they do not generate.


@pytest.mark.parametrize("case,label", [
    ("invalid_date", "日期请使用 YYYY-MM-DD"),
    ("open_day", "这一天还未结束"),
    ("unresolved", "用量尚未确认"),
    ("exhausted", "已达到尝试上限"),
    ("paused", "没有安排日记重算"),
    ("deleted", "没有安排日记重算"),
])
def test_command_explains_recompute_refusal_without_retrying(core, case, label):
    model = seed(core)
    withdraw(core)
    argument = DAY.isoformat()
    if case == "invalid_date":
        argument = "not-a-date"
    elif case == "open_day":
        argument = core.clock.now.astimezone(ZoneInfo(core.settings.timezone)).date().isoformat()
    elif case == "unresolved":
        core.store.execute("UPDATE model_usage SET state='UNKNOWN'")
    elif case == "exhausted":
        core.store.execute("UPDATE diary_runs SET attempts=max_attempts")
    elif case == "paused":
        core.diary.set_mode(False, core.settings.owner_qq_id)
    else:
        core.diary.forget(DAY, core.settings.owner_qq_id)
    assert label in core.diary.command(f"/重算日记 {argument}", core.settings.owner_qq_id)
    assert len(model.calls) == 1
    assert not core.store.one("SELECT 1 FROM diary_runs WHERE state='PENDING'")


async def run_native_diary(core, model_http, monkeypatch, day, expected, during=None):
    """Observe completion while leaving native scheduling/admission/commit code intact."""
    completed = asyncio.Event()
    original = DiaryService.generate_once

    async def observe(service, worker_id):
        assert service.store is not core.store
        worked = await original(service, worker_id)
        row = service.store.one('SELECT state FROM diary_runs WHERE local_date=%s AND timezone=%s',
                                (day.isoformat(), core.settings.timezone))
        if row and row["state"] == expected:
            completed.set()
        return worked

    with monkeypatch.context() as patch:
        patch.setattr(life_worker, "Core", partial(Core, clock=core.clock))
        patch.setattr(life_worker, "ModelClient", lambda cfg, _: ModelClient(cfg, model_http))
        patch.setattr(DiaryService, "generate_once", observe)
        task = asyncio.create_task(life_worker.life_worker(core.settings))
        waiting = asyncio.create_task(completed.wait())
        try:
            if during:
                await during()
            done, _ = await asyncio.wait((task, waiting), timeout=8,
                                         return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                task.result()
            assert waiting in done, core.store.rows("SELECT * FROM diary_runs")
        finally:
            task.cancel()
            waiting.cancel()
            await asyncio.gather(task, waiting, return_exceptions=True)


@pytest.mark.parametrize("entry,recall_during_model", [
    ("api", False), ("command", False), ("api", True),
])
def test_real_http_recall_recompute_and_native_worker_preserve_sources_and_accounting(
    core, tmp_path, monkeypatch, entry, recall_during_model,
):
    core.settings.diary_admin_token = SecretStr(ADMIN_TOKEN)
    source_texts = ["今天讨论了准备撤回的读书计划。", "今天讨论了独立保留的音乐计划。"]
    number, next_text = 0, source_texts[0]
    diary_payloads = []
    model_started, model_release = asyncio.Event(), asyncio.Event()

    async def source(ws):
        nonlocal number
        number += 1
        frame = raw_event(number, next_text, time=int(core.clock.now.timestamp()))
        await ws.send(json.dumps(frame))
        await ws.wait_closed()

    async def provider(request):
        messages = json.loads(request.content)["messages"]
        if messages[0]["content"] == DIARY_RULES:
            data = json.loads(messages[-1]["content"])
            diary_payloads.append(data)
            if recall_during_model and len(diary_payloads) == 2:
                model_started.set()
                await model_release.wait()
            owners = [item for item in data["sources"] if item["kind"] == "owner_message"]
            selected = next((item for item in owners if "准备撤回" in item["text"]), owners[0])
            text = json.dumps({
                "schema_version": 1, "local_date": data["local_date"], "title": "今天的交流",
                "worth_saving": True, "sections": [{
                    "kind": "observation", "text": selected["text"],
                    "evidence": [{"source_id": selected["source_id"],
                                  "exact_quote": selected["text"]}],
                }],
            }, ensure_ascii=False)
        else:
            text = "收到，我们接着聊。"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 140, "completion_tokens": 80},
        })

    async def scenario():
        nonlocal next_text
        options = companion_options(core)
        async with host(core, tmp_path, source, ticking=False,
            purposes=("chat", "diary", "memory", "history"),
            core_options=options["core_options"],
            paid_options={"per_request_calls": 3, "window_calls": 100},
        ) as (window, _, sends, _), httpx.AsyncClient(
            base_url=window.config.core_url,
            headers={"Authorization": "Bearer " + ADMIN_TOKEN}, trust_env=False,
        ) as owner, httpx.AsyncClient(transport=httpx.MockTransport(provider)) as model:
            client = window.client
            core.model = ModelClient(core.settings, model)

            async def capture(text):
                nonlocal next_text
                next_text = text
                owner_authority.current(core.store, core.settings, core.clock, purpose="chat")
                await Capture(core, window.spool, window.runtime).capture_once(
                    client, window.protocol)
                await window.flush()
                return core.store.one(
                    "SELECT event_id FROM events WHERE actor_id='owner' ORDER"
                    ' BY record_sequence DESC LIMIT 1'
                )["event_id"]

            async def recall(message_id):
                assert owner_notices.put(window.runtime,
                    recall_frame(message_id, time=int(core.clock.now.timestamp())),
                    core.clock.now, ("127.0.0.1", 12345))
                assert await owner_notices.flush_one(window.runtime, client)
                assert not await owner_notices.flush_one(window.runtime, client)

            originals = []
            for text in source_texts:
                originals.append(await capture(text))
                assert await core.run_once()
                assert await window.pump()
            local = core.clock.now.astimezone(ZoneInfo(core.settings.timezone))
            day = local.date()
            core.clock.now = (local + timedelta(days=1)).replace(hour=4).astimezone(UTC)
            await run_native_diary(core, model, monkeypatch, day, "COMMITTED")
            original = core.diary.get_day(day, core.settings.owner_qq_id)
            assert "准备撤回" in original["body_md"]
            original_usage = core.store.rows('SELECT * FROM model_usage ORDER BY record_sequence')
            assert len(diary_payloads) == 1
            await recall(1)
            assert core.diary.get_day(day, core.settings.owner_qq_id) is None
            assert core.store.one('SELECT redacted FROM events WHERE event_id=%s',
                                  (originals[1],))["redacted"] == 0
            url = f"/internal/v1/diary/{day}/recompute"
            assert (await client.post(url)).status_code == 401  # Bridge is not diary admin.
            if entry == "api":
                await owner.post("/internal/v1/diary/runtime-mode", json={"enabled": False})
                assert (await owner.post(url)).json()["state"] == "NOT_SCHEDULED"
                await owner.post("/internal/v1/diary/runtime-mode", json={"enabled": True})
                response = await owner.post(url)
                assert response.status_code == 202 and response.json()["state"] == "PENDING"
                assert (await owner.post(url)).json() == response.json()
            else:
                command_event = await capture(f"/重算日记 {day}")
                later_event = await capture("先记着这个问题，继续聊。")
                command_job = core.store.one('SELECT * FROM jobs WHERE event_id=%s',
                                             (command_event,))
                assert command_job["state"] == "QUEUED"
                assert await core.run_once("superseded-control-worker")
                assert core.store.one('SELECT state FROM jobs WHERE job_id=%s',
                                      (command_job["job_id"],))["state"] == "SUCCEEDED"
                assert not core.store.one('SELECT 1 FROM outbox WHERE origin_event_id=%s',
                                          (command_event,))
                assert core.store.one('SELECT state FROM diary_runs WHERE local_date=%s',
                                      (day.isoformat(),))["state"] == "PENDING"
                assert core.store.one('SELECT state FROM jobs WHERE event_id=%s',
                                      (later_event,))["state"] == "QUEUED"
                assert await core.run_once("new-owner-input-worker")
                assert await window.pump()

            async def revoke_in_flight():
                try:
                    await asyncio.wait_for(model_started.wait(), 5)
                    assert (await owner.post(url)).json()["state"] == "RUNNING"
                    await recall(2)
                finally:
                    model_release.set()

            sent_before = len(sends)
            await run_native_diary(
                core, model, monkeypatch, day, "CANCELLED" if recall_during_model else "COMMITTED",
                during=revoke_in_flight if recall_during_model else None,
            )
            assert len(diary_payloads) == 2
            # The model sees short source ids only; the recalled event appears nowhere.
            assert originals[0] not in json.dumps(diary_payloads[-1], ensure_ascii=False)
            assert "准备撤回" not in json.dumps(diary_payloads[-1], ensure_ascii=False)
            if recall_during_model:
                assert core.diary.get_day(day, core.settings.owner_qq_id) is None
            else:
                current = (await owner.get(f"/internal/v1/diary/{day}")).json()
                assert current["entry_id"] == original["entry_id"]
                assert current["version"] == original["version"] + 1
                assert "独立保留" in current["body_md"]
                assert (await owner.post(url)).json()["state"] == "NOT_SCHEDULED"
            assert len(sends) == sent_before  # A private diary is not an outbound notification.
            for usage in original_usage:
                assert core.store.one('SELECT * FROM model_usage WHERE request_id=%s',
                                      (usage["request_id"],)) == usage
            diary_usage = core.store.rows("SELECT * FROM model_usage WHERE scenario='diary'")
            assert len(diary_usage) == 2 and all(item["state"] == "SETTLED" for item in diary_usage)
            assert core.store.one('SELECT attempts FROM diary_runs WHERE local_date=%s',
                                  (day.isoformat(),))["attempts"] == 2
            assert owner_authority.status(
                core.store, core.settings, core.clock)["state"] == "ACTIVE"
            window.runtime.verify()

    try:
        run(scenario())
    except httpx.HTTPStatusError as error:
        raise AssertionError(
            f"{error.request.url.path}: HTTP {error.response.status_code}") from error
