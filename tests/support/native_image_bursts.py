import asyncio
import base64
import io
import json
import re
import sys
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from PIL import Image

from companion import api
from companion.core import Core
from companion_onebot_bridge import app, metrics
from tests.native_continuous_support import host, until
from tests.support.live_image_input import options
from tests.support.onebot_events import raw_event


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
