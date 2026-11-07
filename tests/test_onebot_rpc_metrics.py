"""Offline timing contracts through the original bounded HTTP request boundary."""

import asyncio

import httpx
import pytest

from companion_onebot_bridge import metrics
from companion_onebot_bridge.onebot_client import bounded_post
from tests.conftest import run
from tests.support.onebot_rpc_metrics import CANARY, records

RPCS = [
    ("/internal/v1/owner-authority/check", "t_core_owner_check"),
    ("/internal/v1/owner-authority/inputs/approve", "t_core_input_approve"),
    ("/internal/v1/inbox/events", "t_core_inbox"),
    ("/internal/v1/deliveries/lease", "t_core_delivery_lease"),
    ("/internal/v1/deliveries/msg_" + "a" * 32 + "/begin-send", "t_core_delivery_begin_send"),
    ("/internal/v1/owner-authority/send-check", "t_core_send_check"),
    ("/internal/v1/deliveries/msg_" + "b" * 32 + "/result", "t_core_delivery_result"),
]


def test_opt_in_rpc_spans_preserve_request_order_payloads_and_results(monkeypatch, capsys):
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    runs = []
    for enabled in (False, True):
        monkeypatch.setenv("COMPANION_TELEMETRY", "1" if enabled else "0")
        calls = []

        def respond(request, calls=calls):
            calls.append((request.method, str(request.url), dict(request.headers), request.content))
            return httpx.Response(202, json={"private": CANARY})

        async def scenario():
            async with httpx.AsyncClient(
                base_url="https://private.invalid", headers={"Authorization": "Bearer " + CANARY},
                transport=httpx.MockTransport(respond),
            ) as client:
                return [await bounded_post(client, path, {"text": CANARY}, statuses=(202,),
                                           with_status=True, headers={"X-Idempotency-Key": CANARY})
                        for path, _ in RPCS]

        outcomes = run(scenario())
        runs.append((calls, outcomes))
        rows = records(capsys)
        assert [row["stage"] for row in rows] == [stage for _, stage in RPCS] * enabled
        assert all(row["error_class"] == "none" for row in rows)
    assert runs[0] == runs[1]
    assert len(runs[0][0]) == 7
    assert runs[0][1] == [(202, {"private": CANARY})] * 7


@pytest.mark.parametrize("fault,error_class", [
    ("timeout", "timeout"), ("transport", "transport"), ("http_status", "http_status"),
    ("decode", "validation"), ("size", "validation"), ("unexpected", "unexpected"),
])
def test_rpc_errors_propagate_unchanged_and_emit_safe_classes(
    monkeypatch, capsys, fault, error_class,
):
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    observed = []
    for enabled in (False, True):
        monkeypatch.setenv("COMPANION_TELEMETRY", "1" if enabled else "0")
        calls, failures = [], []

        def respond(request, calls=calls, failures=failures):
            calls.append(request.url.path)
            if fault in {"timeout", "transport", "unexpected"}:
                failure = {"timeout": httpx.ReadTimeout, "transport": httpx.ConnectError,
                           "unexpected": RuntimeError}[fault](CANARY)
                failures.append(failure)
                raise failure
            if fault == "http_status":
                return httpx.Response(503, json={"private": CANARY})
            return httpx.Response(200, content=CANARY if fault == "decode" else "x" * 65537)

        async def scenario(failures=failures):
            async with httpx.AsyncClient(base_url="https://private.invalid",
                                         transport=httpx.MockTransport(respond)) as client:
                with pytest.raises(Exception) as caught:
                    await bounded_post(client, RPCS[0][0], {"private": CANARY})
                if failures:
                    assert caught.value is failures[0]
                return type(caught.value), str(caught.value)

        observed.append((calls, run(scenario())))
        rows = records(capsys)
        assert [row["error_class"] for row in rows] == [error_class] * enabled
        assert all(row["stage"] == "t_core_owner_check" for row in rows)
    assert observed[0] == observed[1]
    assert observed[0][0] == [RPCS[0][0]]


@pytest.mark.parametrize("enabled", [False, True])
def test_cancellation_closes_response_and_remains_cancelled(monkeypatch, capsys, enabled):
    monkeypatch.setenv("COMPANION_TELEMETRY", "1" if enabled else "0")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    calls, closed = [], []

    async def scenario():
        entered = asyncio.Event()

        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b"{}"

            async def aclose(self):
                closed.append(True)

        def respond(request):
            calls.append(request.url.path)
            return httpx.Response(200, stream=WaitingStream())

        async with httpx.AsyncClient(base_url="https://private.invalid",
                                     transport=httpx.MockTransport(respond)) as client:
            task = asyncio.create_task(bounded_post(client, RPCS[-1][0], {"private": CANARY}))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel(CANARY)
            with pytest.raises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(task, 1)
            assert caught.value.args == (CANARY,)
            assert task.cancelled()

    run(scenario())
    assert calls == [RPCS[-1][0]] and closed == [True]
    rows = records(capsys)
    assert [row["error_class"] for row in rows] == ["cancelled"] * enabled


@pytest.mark.parametrize("sink_failure", [OSError, ValueError, RuntimeError])
def test_output_failure_cannot_replace_success_error_or_cancellation(monkeypatch, sink_failure):
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")

    def refused(*args, **kwargs):
        raise sink_failure(CANARY)

    monkeypatch.setattr(metrics, "print", refused, raising=False)
    failures = [httpx.ReadTimeout(CANARY), asyncio.CancelledError(CANARY)]
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if len(calls) > 1:
            raise failures[len(calls) - 2]
        return httpx.Response(200, json={"private": CANARY})

    async def scenario():
        async with httpx.AsyncClient(base_url="https://private.invalid",
                                     transport=httpx.MockTransport(respond)) as client:
            assert await bounded_post(client, RPCS[0][0], {}) == {"private": CANARY}
            for failure in failures:
                with pytest.raises(type(failure)) as caught:
                    await bounded_post(client, RPCS[0][0], {})
                assert caught.value is failure

    run(scenario())
    assert calls == [RPCS[0][0]] * 3


def test_span_includes_request_hook_response_body_and_close(monkeypatch, capsys):
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    clock = [1000000]
    monkeypatch.setattr(metrics.time, "perf_counter_ns", lambda: clock[0])

    async def hook(request):
        clock[0] += 3000000

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            clock[0] += 11000000
            yield b'{"ok":true}'

        async def aclose(self):
            clock[0] += 13000000

    def respond(request):
        clock[0] += 7000000
        return httpx.Response(200, stream=Body())

    async def scenario():
        async with httpx.AsyncClient(base_url="https://private.invalid",
                                     event_hooks={"request": [hook]},
                                     transport=httpx.MockTransport(respond)) as client:
            assert await bounded_post(client, RPCS[0][0], {}) == {"ok": True}

    run(scenario())
    row, = records(capsys)
    assert row["elapsed_ms"] == 34 and row["monotonic_ns"] == 35000000


def test_non_allowlisted_paths_emit_nothing_and_still_make_original_requests(monkeypatch, capsys):
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")
    paths = [
        "/get_status", "/send_private_msg", "/internal/v1/account-actions/send-check",
        "/internal/v1/owner-authority/check-extra",
        "/internal/v1/owner-authority/check?private=" + CANARY,
        "/internal/v1/deliveries/" + CANARY + "/result",
        "/internal/v1/deliveries/msg_" + "a" * 31 + "/begin-send",
        RPCS[-1][0] + "/extra", "https://private.invalid" + RPCS[0][0],
        httpx.URL("https://private.invalid" + RPCS[0][0]),
    ]
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    async def scenario():
        async with httpx.AsyncClient(base_url="https://private.invalid",
                                     transport=httpx.MockTransport(respond)) as client:
            for path in paths:
                assert await bounded_post(client, path, {}) == {}

    run(scenario())
    assert len(calls) == len(paths) and capsys.readouterr().out == ""
