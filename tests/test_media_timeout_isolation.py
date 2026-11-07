"""A failed native media Core guard must not cancel receive/send sibling tasks."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from companion.core import Core
from companion_onebot_bridge import app, pending_media
from tests.conftest import run
from tests.native_continuous_support import until
from tests.support.native_image_bursts import (
    bridge_tasks,
    model_images,
    native_host,
    observe_delivery,
    resolve,
    send,
    wait_for_ack,
)
from tests.support.onebot_images import image_segment
from tests.support.owner_image_recovery import run_worker


@pytest.mark.parametrize("failure", ["outer_timeout", "transport"])
def test_native_media_core_guard_failure_keeps_task_group_and_next_turn_alive(
    core, tmp_path, monkeypatch, failure,
):
    async def scenario():
        async with native_host(core, tmp_path, monkeypatch, count=1) as values:
            active, inbound, handshakes, sends, calls = values
            failed, unwound = asyncio.Event(), asyncio.Event()
            requests, downloads = [], []
            retries, responses, rpc = observe_delivery(active, monkeypatch)

            async def before(request):
                requests.append(request.url.path)
                if not request.url.path.endswith("/image/check") or failed.is_set():
                    return
                if not pending_media.exists(active.runtime):
                    return
                row = active.db.execute("SELECT state FROM automatic_media").fetchone()
                if row is None or row[0] != "DOWNLOADING":
                    return
                failed.set()
                try:
                    if failure == "outer_timeout":
                        async with asyncio.timeout(0):
                            await asyncio.sleep(1)
                    raise httpx.ReadError("synthetic private Core guard failure", request=request)
                finally:
                    unwound.set()

            def cdn(request):
                downloads.append(request)
                pytest.fail("Core guard failed before any CDN request; no download may be replayed")

            active.client.event_hooks["request"].append(before)
            pump = pending_media.MediaPump(active.runtime, active.client,
                image_transport=httpx.MockTransport(cdn), image_resolver=resolve,
                metrics=active.metrics)
            async with bridge_tasks(active, pump) as tasks:
                await send(core, inbound, 1, [
                    {"type": "text", "data": {"text": "图片失败也请保留这段原文"}},
                    image_segment("https://gchat.qpic.cn/guard-timeout"),
                ])
                await until(unwound.is_set, tasks=tasks, deadline_seconds=20)
                original = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
                core.clock.advance(61)
                with Core(core.settings, core.model, core.clock) as independent:
                    assert independent.store is not core.store
                    await independent.content.run_once()
                await until(lambda: active.db.execute("SELECT state FROM automatic_media")
                    .fetchone()[0] == "DONE", tasks=tasks, deadline_seconds=20)
                saved = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
                assert saved["state"] == "FAILED"
                assert saved["error_code"] == "CONTENT_IMAGE_DOWNLOAD_EXPIRED"
                for key in ("lease_token", "lease_until", "reserved_bytes"):
                    assert saved[key] == original[key]
                assert not downloads and not core.store.rows("SELECT * FROM captured_images")
                assert sum(path.endswith("/image/claim") for path in requests) == 1
                assert not any(path.endswith("/image") for path in requests)
                first = await run_worker(core, tmp_path, 1, "原文已收到，图片暂时不可用。")
                assert model_images(first[0]) == []
                assert "图片失败也请保留这段原文" in json.dumps(
                    first[0]["messages"], ensure_ascii=False)
                await wait_for_ack(core, active, sends, tasks, 1, retries, responses, rpc)
                await send(core, inbound, 2, [{"type": "text", "data": {"text": "继续聊天"}}])
                await until(lambda: len(core.store.rows("SELECT * FROM events")) == 2,
                            tasks=tasks, deadline_seconds=20)
                await run_worker(core, tmp_path, 2, "下一条也收到了。")
                await wait_for_ack(core, active, sends, tasks, 2, retries, responses, rpc)
                assert active.runtime.row()["state"] == "ACTIVE"
                assert all(not task.done() for task in tasks)
                assert len(handshakes) == 1 and len(sends) == 2 and calls == []
                assert not active.db.execute(
                    "SELECT 1 FROM window_attempts WHERE state='UNKNOWN'").fetchone()
                assert core.store.rows("SELECT state FROM model_usage") == [
                    {"state": "SETTLED"}, {"state": "SETTLED"}]
    run(scenario())


@pytest.mark.parametrize("failure", ["permission", "integrity", "cancelled", "legacy_timeout"])
def test_media_boundary_keeps_fatal_checks_and_cancellation(failure):
    request = httpx.Request("POST", "http://synthetic.invalid/check")
    error = {
        "permission": httpx.HTTPStatusError("denied", request=request,
                                             response=httpx.Response(403, request=request)),
        "integrity": ValueError("INVALID_CORE_RECEIPT"),
        "cancelled": asyncio.CancelledError(),
        "legacy_timeout": TimeoutError(),
    }[failure]
    closed, unwound = [], []

    class Pump:
        def __init__(self):
            self.window = SimpleNamespace(verify=lambda: None,
                config=SimpleNamespace(poll_seconds=0.01))
            if failure != "legacy_timeout":
                self.window.authority = {"version": 2}
            self.task = None
            self.metrics = None

        def tick(self):
            async def transfer():
                try:
                    raise error
                finally:
                    unwound.append(True)
            self.task = asyncio.create_task(transfer())

        async def close(self):
            assert self.task.done() and unwound
            await asyncio.gather(self.task, return_exceptions=True)
            closed.append(True)

    async def scenario():
        with pytest.raises(type(error)):
            await app.transfer_media(Pump(), asyncio.Event())
        assert closed == [True]
    run(scenario())
