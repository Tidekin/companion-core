"""Structured, bounded read-only probes; all transports and payloads are synthetic."""

import asyncio
import json
import math

import httpx
import pytest

from companion_onebot_bridge.onebot_client import OneBotClient, http_client
from tests.conftest import run
from tests.support.onebot_events import config

DATA = {
    "/get_login_info": {"user_id": 10002},
    "/get_status": {"online": True, "good": True},
    "/get_version_info": {"app_name": "SnowLuma", "app_version": "2.0.3-node"},
}


def accepted(request, changes=None):
    return httpx.Response(200, json={"status": "ok", "retcode": 0,
        "data": (changes or {}).get(request.url.path, DATA[request.url.path])})


def test_three_requests_overlap_and_repeat_without_caching():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def respond(request):
            assert request.method == "POST" and json.loads(request.content) == {}
            assert request.headers["Authorization"] == "Bearer " + config().onebot_token
            requests.append(request.url.path)
            if len(requests) % 3 == 0:
                entered.set()
            await release.wait()
            return accepted(request)

        async with http_client(config().onebot_http_url, config().onebot_token,
                               transport=httpx.MockTransport(respond)) as client:
            onebot = OneBotClient(config(), client)
            for _ in range(2):
                task = asyncio.create_task(onebot.probe())
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    assert not task.done()
                    release.set()
                    result = await asyncio.wait_for(task, 1)
                    assert result == {"account_matches": True, "online": True, "good": True,
                        "expected_version_reported": True, "connection_ready": True,
                        "private_message_delivery_verified": False}
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                entered.clear()
                release.clear()
        assert sorted(requests) == sorted(list(DATA) * 2)
    run(scenario())


@pytest.mark.parametrize("change", [
    {"/get_login_info": {"user_id": 99999}},
    {"/get_status": {"online": False, "good": True}},
    {"/get_status": {"online": True, "good": False}},
    {"/get_status": {"online": 1, "good": True}},
    {"/get_version_info": {"app_name": "other", "app_version": "2.0.3-node"}},
    {"/get_version_info": {"app_name": "SnowLuma", "app_version": "other"}},
])
def test_all_original_aggregate_requirements_remain_required(change):
    async def scenario():
        seen = []

        def respond(request):
            seen.append(request.url.path)
            return accepted(request, change)

        async with http_client(config().onebot_http_url, config().onebot_token,
                               transport=httpx.MockTransport(respond)) as client:
            assert not (await OneBotClient(config(), client).probe())["connection_ready"]
        assert sorted(seen) == sorted(DATA)
    run(scenario())


@pytest.mark.parametrize("fault", ["validation", "http_status", "transport", "timeout"])
def test_failure_preserves_leaf_class_and_awaits_sibling_cleanup(fault):
    async def scenario():
        entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        tasks, cleaning, closed = [], set(), set()
        request = httpx.Request("POST", "http://127.0.0.1/get_status")
        error = {
            "validation": ValueError("INVALID_ONEBOT_ACCEPTANCE"),
            "http_status": httpx.HTTPStatusError("private", request=request,
                                                 response=httpx.Response(401)),
            "transport": httpx.ConnectError("private", request=request),
            "timeout": httpx.ReadTimeout("private", request=request),
        }[fault]

        async def respond(request):
            tasks.append(asyncio.current_task())
            if len(tasks) == 3:
                entered.set()
            await entered.wait()
            if request.url.path == "/get_status":
                raise error
            try:
                await asyncio.Future()
            finally:
                cleaning.add(request.url.path)
                if len(cleaning) == 2:
                    cleanup.set()
                await release.wait()
                closed.add(request.url.path)

        async with http_client(config().onebot_http_url, config().onebot_token,
                               transport=httpx.MockTransport(respond)) as client:
            task = asyncio.create_task(OneBotClient(config(), client).probe())
            try:
                await asyncio.wait_for(cleanup.wait(), 1)
                assert not task.done() and not closed
                release.set()
                with pytest.raises(type(error)) as caught:
                    await asyncio.wait_for(task, 1)
                assert caught.value is error
                assert isinstance(error.__cause__, ExceptionGroup)
                assert error.__cause__.exceptions == (error,)
                assert len(closed) == 2 and all(item.done() for item in tasks)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    run(scenario())


def test_simultaneous_failures_retain_the_complete_group_as_cause():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        tasks = []
        errors = {path: ValueError(path) for path in DATA}

        async def respond(request):
            tasks.append(asyncio.current_task())
            if len(tasks) == 3:
                entered.set()
            await release.wait()
            raise errors[request.url.path]

        async with http_client(config().onebot_http_url, config().onebot_token,
                               transport=httpx.MockTransport(respond)) as client:
            task = asyncio.create_task(OneBotClient(config(), client).probe())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                release.set()
                with pytest.raises(ValueError) as caught:
                    await asyncio.wait_for(task, 1)
                group = caught.value.__cause__
                assert isinstance(group, ExceptionGroup)
                assert set(group.exceptions) == set(errors.values())
                assert all(item.done() for item in tasks)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    run(scenario())


def test_parent_cancellation_waits_for_all_open_response_streams_to_close():
    async def scenario():
        entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        tasks, closing, closed = [], [], []

        class HeldStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                tasks.append(asyncio.current_task())
                if len(tasks) == 3:
                    entered.set()
                await asyncio.Future()
                yield b"unreachable"

            async def aclose(self):
                closing.append(self)
                if len(closing) == 3:
                    cleanup.set()
                await release.wait()
                closed.append(self)

        async with http_client(config().onebot_http_url, config().onebot_token,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=HeldStream())),
        ) as client:
            task = asyncio.create_task(OneBotClient(config(), client).probe())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                await asyncio.wait_for(cleanup.wait(), 1)
                assert not task.done() and not closed
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                assert len(closed) == 3 and all(item.done() for item in tasks)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    run(scenario())


def test_probe_metrics_are_opt_in_and_have_only_fixed_sanitized_fields(monkeypatch, capsys):
    from companion_onebot_bridge import metrics

    private = "Bearer private-secret https://private.invalid/account?token=private-secret"
    monkeypatch.delenv("COMPANION_TELEMETRY", raising=False)
    metrics.probe_finished("get_status", 0, ValueError(private))
    assert capsys.readouterr().out == ""
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    request = httpx.Request("POST", "https://private.invalid/secret")
    errors = [None, asyncio.CancelledError(private), ValueError(private),
              httpx.ReadTimeout(private), httpx.ConnectError(private),
              httpx.HTTPStatusError(private, request=request, response=httpx.Response(401)),
              RuntimeError(private)]
    for error in errors:
        metrics.probe_finished("get_status", 0, error)
    output = capsys.readouterr().out
    records = [json.loads(line) for line in output.splitlines()]
    assert [record["error_class"] for record in records] == [
        "none", "cancelled", "validation", "timeout", "transport", "http_status", "unexpected"]
    assert all(set(record) == {"schema_version", "evidence_type", "stage", "monotonic_ns",
                               "elapsed_ms", "error_class"} for record in records)
    assert all(record["stage"] == "t_onebot_probe_status"
               and record["evidence_type"] == "OFFLINE_FIXTURE"
               and math.isfinite(record["elapsed_ms"]) and record["elapsed_ms"] >= 0
               for record in records)
    assert "private" not in output and "Authorization" not in output


@pytest.mark.parametrize("broken_output", [False, True])
def test_actual_probe_emits_three_spans_and_output_failure_cannot_change_result(
    monkeypatch, capsys, broken_output,
):
    from companion_onebot_bridge import metrics

    monkeypatch.setenv("COMPANION_TELEMETRY", "1")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    if broken_output:
        def refused(*args, **kwargs):
            raise OSError("private output failure")

        monkeypatch.setattr(metrics, "print", refused, raising=False)

    async def scenario():
        async with http_client(config().onebot_http_url, config().onebot_token,
                               transport=httpx.MockTransport(accepted)) as client:
            assert (await OneBotClient(config(), client).probe())["connection_ready"]
    run(scenario())
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    if broken_output:
        assert not records
    else:
        assert {record["stage"] for record in records} == {
            "t_onebot_probe_login", "t_onebot_probe_status", "t_onebot_probe_version"}
        assert len(records) == 3 and all(record["error_class"] == "none" for record in records)
