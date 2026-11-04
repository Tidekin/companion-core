"""Real native receive/media/send tasks, Core HTTP and independent SDK workers."""

import asyncio
import base64
import hashlib
import io
import json
import re
import sys
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from companion import api
from companion.core import Core
from companion_onebot_bridge import app, metrics, pending_media
from companion_onebot_bridge.onebot_client import bounded_post
from tests.conftest import run
from tests.native_continuous_support import host, until
from tests.support.onebot_events import raw_event
from tests.test_live_image_input import options
from tests.test_onebot_images import image_segment
from tests.test_owner_image_recovery import run_worker


def picture(color):
    output = io.BytesIO()
    Image.new("RGB", (3, 2), color).save(output, format="PNG")
    return output.getvalue()


def model_images(request):
    return [base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
            for message in request["messages"] if isinstance(message["content"], list)
            for part in message["content"] if part["type"] == "image_url"]


@asynccontextmanager
async def bridge_tasks(active, pump):
    async with asyncio.TaskGroup() as group:
        tasks = [
            group.create_task(app.receive(active.config, active.spool, active.wake, active.stop,
                active.metrics, active.onebot, runtime=active.runtime, core=active.client)),
            group.create_task(app.upload(active.client, active.spool, active.wake, active.stop,
                active.metrics, runtime=active.runtime)),
            group.create_task(app.deliver(active.client, active.onebot, active.spool, active.stop,
                active.metrics, runtime=active.runtime, protocol=active.protocol)),
            group.create_task(app.transfer_media(pump, active.stop)),
        ]
        try:
            yield tasks
        finally:
            active.stop.set()
            for task in tasks:
                task.cancel()


@asynccontextmanager
async def native_host(
    core, tmp_path, monkeypatch, *, count, history=False, poll_seconds=None, wait_seconds=30,
):
    inbound = asyncio.Queue()

    async def source(ws):
        async def send():
            while True:
                await ws.send(await inbound.get())
        sender = asyncio.create_task(send())
        try:
            await ws.wait_closed()
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    monkeypatch.setattr(api, "Core", lambda *args, **kwargs: Core(
        *args, clock=core.clock, **kwargs))
    scope = options()
    scope["core_options"]["content"] = scope["core_options"]["content"].model_copy(
        update={"max_images_per_event": count, "wait_seconds": wait_seconds})
    scope["paid_options"]["image_input"] = scope["paid_options"]["image_input"].model_copy(
        update={"max_images_per_request": count})
    bridge_options = {"rich_input_scope": "references_v1"}
    if poll_seconds is not None:
        bridge_options["poll_seconds"] = poll_seconds
    async with host(core, tmp_path, source,
        core_options={**scope["core_options"], "rich_input_scope": "references_v1"},
        paid_options=scope["paid_options"], purposes=("chat", "image", "content",
            *(('history',) if history else ())),
        bridge_options=bridge_options, independent_api=True,
    ) as (active, handshakes, sends, calls):
        core.content.options = core.settings.content
        yield active, inbound, handshakes, sends, calls


async def send(core, inbound, number, message):
    await inbound.put(json.dumps(raw_event(number, time=int(datetime.now(UTC).timestamp()),
                                            message=message), ensure_ascii=False))


async def resolve(*_):
    return ["1.1.1.1"]


def observe_delivery(active, monkeypatch):
    retries, responses = Counter(), Counter()
    sink = active.metrics.sink
    rpc = {"errors": [], "last": {}}
    anchor = time.perf_counter_ns()
    rpc_sink = metrics._request_finished
    stages = set(metrics.CORE_RPC_STAGES.values()) | set(
        metrics.DELIVERY_RPC_STAGES.values()) | set(metrics.PROBE_STAGES.values())

    def rpc_observed(stage, started_ns, error=None):
        rpc_sink(stage, started_ns, error)
        if stage in stages:
            span = {"stage": stage, "started_ms": round((started_ns - anchor) / 1e6, 4),
                    "finished_ms": round((time.perf_counter_ns() - anchor) / 1e6, 4),
                    "error_class": type(error).__name__ if error is not None else None}
            rpc["last"][stage] = span
            # Later idle polling must not overwrite the first failed operation.
            if error is not None and len(rpc["errors"]) < 8:
                rpc["errors"].append(span)

    monkeypatch.setattr(metrics, "_request_finished", rpc_observed)

    def observed(record):
        sink(record)
        if record["stage"] == "send_unavailable":
            error = sys.exception()
            status = error.response.status_code if isinstance(
                error, httpx.HTTPStatusError) else 0
            retries[f"{type(error).__name__}:{status}"] += 1

    active.metrics.sink = observed

    async def response_observed(response):
        path = response.request.url.path
        phase = next((phase for phase in (
            "lease", "begin-send", "result", "send-check", "cancel-before-send", "check",
        ) if path.endswith("/" + phase)), None)
        if phase:
            responses[f"{phase}:{response.status_code}"] += 1
            size = response.headers.get("Content-Length", "")
            if response.is_error and size.isdecimal() and int(size) <= 512:
                await response.aread()
                try:
                    code = response.json().get("error")
                except (ValueError, AttributeError):
                    code = None
                if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code):
                    responses[f"{phase}:{response.status_code}:{code}"] += 1

    active.client.event_hooks["response"].append(response_observed)
    return retries, responses, rpc


async def wait_for_ack(core, active, sends, tasks, count, retries, responses, rpc):
    try:
        await until(lambda: active.attempts() == {"ACK": count}, tasks=tasks,
                    deadline_seconds=20)
    except TimeoutError as error:
        now = core.clock()
        evidence = {
            "expected_acks": count, "attempts": active.attempts(),
            "sends": len(sends), "runtime": active.runtime.row()["state"],
            "stop": active.stop.is_set(),
            "stop_marker": active.runtime.stop_path.exists(),
            "clock_minus_wall_seconds": (core.clock.now - datetime.now(UTC)
                                         ).total_seconds(),
            "outbox": core.store.rows(
                "SELECT state,last_error_code,attempt_count,"
                "(julianday(lease_until)-julianday(?))*86400 lease_seconds,"
                "(julianday(next_attempt_at)-julianday(?))*86400 due_seconds "
                "FROM outbox ORDER BY rowid", (now, now)),
            "jobs": core.store.rows(
                "SELECT kind,state,last_error_code FROM jobs ORDER BY rowid"),
            "retry_errors": dict(retries), "http_responses": dict(responses),
            "rpc_spans": rpc,
            "tasks": [{
                "done": task.done(), "cancelled": task.cancelled(),
                "error_class": (type(task.exception()).__name__ if task.done()
                    and not task.cancelled() and task.exception() else None),
                "stack": [f"{frame.f_code.co_name}:{frame.f_lineno}"
                          for frame in task.get_stack()],
            } for task in tasks],
        }
        # The assertion survives TaskGroup cleanup and capsys consumption in CI.
        # Project only states/codes/counts, never bodies, URLs or exception text.
        raise AssertionError("NATIVE_IMAGE_ACK_TIMEOUT " + json.dumps(
            evidence, sort_keys=True)) from error


@pytest.mark.parametrize("failure", ["connection", "read_timeout", "log_failure"])
def test_failed_picture_keeps_task_group_alive_for_sibling_image_text_and_next_turn(
    core, tmp_path, monkeypatch, capsys, failure,
):
    async def scenario():
        async with native_host(core, tmp_path, monkeypatch, count=2, wait_seconds=90) as values:
            active, inbound, handshakes, sends, calls = values
            failed, downloads, requests = asyncio.Event(), [], []
            retries, responses, rpc = observe_delivery(active, monkeypatch)
            good = picture("green")

            def cdn(request):
                downloads.append(request.url.path)
                if request.url.path.endswith("/bad"):
                    failed.set()
                    private = str(request.url) + " synthetic-owner-private-detail"
                    if failure != "read_timeout":
                        raise httpx.ConnectError(private, request=request)
                    raise httpx.ReadTimeout(private, request=request)
                return httpx.Response(200, content=good, headers={"Content-Type": "image/png"})

            async def tracked(request):
                if request.url.path.endswith(("/image/claim", "/image")):
                    requests.append(request.url.path)

            active.client.event_hooks["request"].append(tracked)
            if failure == "log_failure":
                from companion_onebot_bridge import continuous

                def unavailable(*args, **kwargs):
                    raise OSError("synthetic telemetry sink unavailable")

                monkeypatch.setattr(continuous, "emit", unavailable)
            pump = pending_media.MediaPump(active.runtime, active.client,
                image_transport=httpx.MockTransport(cdn), image_resolver=resolve,
                metrics=active.metrics)
            async with bridge_tasks(active, pump) as tasks:
                await send(core, inbound, 1, [
                    {"type": "text", "data": {"text": "保留这段文字，并看看附件"}},
                    image_segment("https://gchat.qpic.cn/bad?rkey=synthetic-signed-key", subtype=1),
                    image_segment("https://gchat.qpic.cn/good", subtype=1),
                ])
                await until(failed.is_set, tasks=tasks, deadline_seconds=20)
                original = core.store.one("SELECT * FROM content_items WHERE slot=0")
                core.clock.advance(61)
                await core.content.run_once()
                await until(lambda: active.db.execute("SELECT state FROM automatic_media")
                    .fetchone()[0] == "DONE", tasks=tasks, deadline_seconds=20)
                assert [row["state"] for row in core.store.rows(
                    "SELECT state FROM content_items ORDER BY slot")] == ["FAILED", "SUCCEEDED"]
                closed = core.store.one("SELECT * FROM content_items WHERE slot=0")
                assert closed["reserved_bytes"] == original["reserved_bytes"]
                assert closed["lease_token"] == original["lease_token"]
                assert closed["lease_until"] == original["lease_until"]
                first = await run_worker(core, tmp_path, 1, "文字已收到，一张图片暂时读不到。")
                assert len(first) == 1 and model_images(first[0]) == [good]
                assert "保留这段文字" in json.dumps(first[0]["messages"], ensure_ascii=False)
                await wait_for_ack(core, active, sends, tasks, 1, retries, responses, rpc)
                await send(core, inbound, 2, [{"type": "text", "data": {"text": "继续聊天"}}])
                await until(lambda: len(core.store.rows("SELECT * FROM events")) == 2,
                            tasks=tasks, deadline_seconds=20)
                second = await run_worker(core, tmp_path, 2, "可以，继续聊。")
                assert len(second) == 1 and model_images(second[0]) == []
                await wait_for_ack(core, active, sends, tasks, 2, retries, responses, rpc)
                assert active.runtime.row()["state"] == "ACTIVE"
                assert all(not task.done() for task in tasks)
                assert len(handshakes) == 1 and len(sends) == 2 and calls == []
                assert downloads == ["/bad", "/good"]
                assert sum(path.endswith("/image/claim") for path in requests) == 2
                assert sum(path.endswith("/image") for path in requests) == 1
                assert [row["stage"] for row in active.events].count("media_unavailable") == 1
                assert core.store.rows("SELECT scenario,state FROM model_usage") == [
                    {"scenario": "chat", "state": "SETTLED"},
                    {"scenario": "chat", "state": "SETTLED"},
                ]
                active.runtime.verify()
    run(scenario())
    output = capsys.readouterr().out
    if failure != "log_failure":
        assert '"event": "media_download_failed"' in output
        assert ("IMAGE_FETCH_FAILED" if failure == "read_timeout" else
                "IMAGE_CONNECTION_FAILED") in output
    assert "synthetic-signed-key" not in output and "synthetic-owner-private-detail" not in output


@pytest.mark.parametrize("path,stage", [
    ("/internal/v1/owner-authority/check", "t_core_owner_check"),
    ("/internal/v1/deliveries/msg_" + "a" * 32 + "/begin-send",
     "t_core_delivery_begin_send"),
], ids=["check", "begin-send"])
def test_delivery_observer_retains_failed_operation_after_successful_poll(
    monkeypatch, path, stage,
):
    canary = "synthetic-owner-private-detail"
    calls, failures = [], []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            failures.append(httpx.ReadError(canary, request=request))
            raise failures[0]
        return httpx.Response(200, json={})

    async def scenario():
        async with httpx.AsyncClient(base_url="http://127.0.0.1:1234",
            transport=httpx.MockTransport(respond)) as client:
            active = SimpleNamespace(client=client, metrics=metrics.Metrics(sink=lambda _: None))
            _, _, rpc = observe_delivery(active, monkeypatch)
            with pytest.raises(httpx.ReadError) as caught:
                await bounded_post(client, path, {"private": canary})
            assert caught.value is failures[0]
            assert await bounded_post(client, path, {}) == {}
            return rpc

    rpc = run(scenario())
    assert len(calls) == 2 and len(rpc["errors"]) == 1
    failed = rpc["errors"][0]
    assert failed["stage"] == stage and failed["error_class"] == "ReadError"
    assert 0 <= failed["started_ms"] <= failed["finished_ms"]
    assert rpc["last"][stage]["error_class"] is None
    assert not any(value in json.dumps(rpc)
                   for value in (canary, "127.0.0.1", "/internal/", "msg_"))


@pytest.mark.parametrize("fault", ["core_transport", "preflight"])
def test_ack_timeout_retains_unsent_states_and_safe_retry_diagnostics(
    core, tmp_path, monkeypatch, capsys, fault,
):
    original_host = native_host

    @asynccontextmanager
    async def failing_host(*args, **kwargs):
        async with original_host(*args, **kwargs) as values:
            async def refuse(request):
                if request.url.path == "/internal/v1/deliveries/lease":
                    raise httpx.ConnectError("synthetic-signed-key", request=request)

            if fault == "core_transport":
                values[0].client.event_hooks["request"].append(refuse)
            yield values

    if fault == "preflight":
        from companion_onebot_bridge.owner_send import OwnerOneBot

        async def unavailable(self):
            raise httpx.ConnectError("synthetic-owner-private-detail")

        monkeypatch.setattr(OwnerOneBot, "probe", unavailable)
    monkeypatch.setattr(sys.modules[__name__], "native_host", failing_host)
    with pytest.raises(ExceptionGroup) as caught:
        test_failed_picture_keeps_task_group_alive_for_sibling_image_text_and_next_turn(
            core, tmp_path, monkeypatch, capsys, "log_failure")
    assert len(caught.value.exceptions) == 1
    error = caught.value.exceptions[0]
    assert isinstance(error, AssertionError) and isinstance(error.__cause__, TimeoutError)
    message = str(error)
    assert message.startswith("NATIVE_IMAGE_ACK_TIMEOUT ")
    assert "synthetic-signed-key" not in message and "synthetic-owner-private-detail" not in message
    evidence = json.loads(message.removeprefix("NATIVE_IMAGE_ACK_TIMEOUT "))
    assert evidence["expected_acks"] == 1 and evidence["sends"] == 0
    assert evidence["runtime"] == "ACTIVE" and not evidence["stop"] and not evidence["stop_marker"]
    assert all(not task["done"] and task["stack"] for task in evidence["tasks"])
    assert evidence["jobs"] == [{"kind": "chat", "state": "SUCCEEDED", "last_error_code": None}]
    assert isinstance(evidence["clock_minus_wall_seconds"], float)
    if fault == "core_transport":
        assert evidence["attempts"] == {} and evidence["outbox"][0]["state"] == "PENDING"
        assert evidence["retry_errors"]["ConnectError:0"] > 0
        assert evidence["rpc_spans"]["errors"][0]["stage"] == "t_core_delivery_lease"
        assert evidence["rpc_spans"]["errors"][0]["error_class"] == "ConnectError"
    else:
        assert evidence["attempts"] == {}, evidence
        assert evidence["outbox"][0]["state"] == "LEASED", evidence
        assert evidence["outbox"][0]["last_error_code"] is None
        assert not any(key.startswith("cancel-before-send:")
                       for key in evidence["http_responses"])
        assert evidence["retry_errors"]["ConnectError:0"] == 1


@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("mode", ["downloading", "staged", "same_message"])
def test_image_burst_and_latest_text_keep_all_originals_in_order(
    core, tmp_path, monkeypatch, count, mode,
):
    async def scenario():
        async with native_host(core, tmp_path, monkeypatch, count=count, history=True) as values:
            active, inbound, handshakes, sends, calls = values
            retries, responses, rpc = observe_delivery(active, monkeypatch)
            originals = [picture(color) for color in ("red", "blue", "green")][:count]
            started, release, downloads = asyncio.Event(), asyncio.Event(), []
            question = "结合这些图片，按发送顺序说明区别"

            async def cdn(request):
                index = int(request.url.path.rsplit("/", 1)[1])
                downloads.append(index)
                if mode != "staged" and index == count // 2:
                    started.set()
                    await release.wait()
                return httpx.Response(200, content=originals[index],
                                      headers={"Content-Type": "image/png"})

            pump = pending_media.MediaPump(active.runtime, active.client,
                image_transport=httpx.MockTransport(cdn), image_resolver=resolve)
            async with bridge_tasks(active, pump) as tasks:
                segments = [image_segment(f"https://gchat.qpic.cn/original/{i}", subtype=1)
                            for i in range(count)]
                if mode == "same_message":
                    await send(core, inbound, 1, [
                        {"type": "text", "data": {"text": question}}, *segments])
                    await until(started.is_set, tasks=tasks, deadline_seconds=20)
                    total_inputs = 1
                else:
                    for index, segment in enumerate(segments):
                        await send(core, inbound, index + 1, [segment])
                        await until(lambda n=index + 1: active.captures() == n, tasks=tasks,
                                    deadline_seconds=20)
                        if mode == "staged":
                            await until(lambda n=index + 1: core.store.one(
                                "SELECT COUNT(*) n FROM captured_images")["n"] == n,
                                tasks=tasks, deadline_seconds=20)
                    if mode == "downloading":
                        await until(started.is_set, tasks=tasks, deadline_seconds=20)
                    await send(core, inbound, count + 1,
                               [{"type": "text", "data": {"text": question}}])
                    total_inputs = count + 1
                await until(lambda: len(core.store.rows("SELECT * FROM events")) == total_inputs,
                            tasks=tasks, deadline_seconds=20)
                job = core.store.one("SELECT * FROM jobs WHERE kind='chat' ORDER BY rowid DESC")
                if mode != "staged":
                    assert core.claim_job("wait-for-originals", job_id=job["job_id"]) is None
                release.set()
                await until(lambda: len(core.store.rows("SELECT * FROM captured_images")) == count,
                            tasks=tasks, deadline_seconds=20)
                request = await run_worker(core, tmp_path, 1, "按原图顺序进行合成回答。")
                assert len(request) == 1 and model_images(request[0]) == originals
                assert question in json.dumps(request[0]["messages"], ensure_ascii=False)
                await wait_for_ack(core, active, sends, tasks, 1, retries, responses, rpc)
                assert [row["origin_event_id"] for row in core.store.rows("SELECT * FROM outbox")
                        ] == [job["event_id"]]
                assert len(sends) == len(handshakes) == 1 and calls == []
                assert downloads == list(range(count))
                assert core.store.rows("SELECT scenario,state FROM model_usage") == [
                    {"scenario": "chat", "state": "SETTLED"}]
                assert [row["media_sha256"] for row in core.store.rows(
                    "SELECT c.media_sha256 FROM agent_image_inputs a JOIN content_items c "
                    "USING(item_id) WHERE a.job_id=? ORDER BY a.position", (job["job_id"],))] == [
                    hashlib.sha256(data).hexdigest() for data in originals]
                assert active.runtime.row()["state"] == "ACTIVE"
                active.runtime.verify()
    run(scenario())
