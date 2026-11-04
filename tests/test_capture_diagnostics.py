"""Fixed diagnostic codes and SnowLuma lifecycle frames; synthetic transports only."""

import asyncio
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from companion_onebot_bridge import app, capture
from companion_onebot_bridge import window as window_module
from tests.conftest import run
from tests.support.onebot_events import raw_event
from tests.test_onebot_capture import bootstrap_frames, source_window


def cli_failure(monkeypatch, capsys, error):
    async def command(*args):
        raise error

    monkeypatch.setattr(app.BridgeConfig, "load", lambda path: object())
    monkeypatch.setattr(window_module, "command", command)
    monkeypatch.setattr(sys, "argv", ["bridge", "window-capture-once"])
    with pytest.raises(SystemExit) as stopped:
        app.main()
    assert stopped.value.code == 1
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    return output, json.loads(output)


def lifecycle_frames():
    frames = bootstrap_frames()
    for frame in frames[:2]:
        frame["status"] = {"online": True, "good": True}
    return frames


def test_snowluma_status_lifecycle_bootstrap_then_owner_text(core, tmp_path):
    async def source(ws):
        for frame in lifecycle_frames():
            await ws.send(json.dumps(frame))
        await ws.send(json.dumps(raw_event()))
        await ws.wait_closed()

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, sends):
            result = await window.capture_once(client, protocol)
            assert result["new_inputs"] == 1 and result["state"] == "OPEN"
            assert window.db.execute("SELECT state FROM window_inputs").fetchone()[0] == "CAPTURED"
            assert core.model.calls == sends == []
            assert not core.store.one("SELECT 1 FROM events")

    run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"status": {"online": False, "good": True}},
        {"status": {"online": True, "good": 1}},
        {"status": {"online": True}},
        {"status": {"online": True, "good": True, "unknown": True}},
        {"status": []},
        {"unknown": True},
        {"sub_type": "disable"},
    ],
)
def test_lifecycle_status_unknown_fields_and_disable_reject(core, tmp_path, change):
    async def source(ws):
        await ws.send(json.dumps(lifecycle_frames()[0] | change))
        await ws.wait_closed()

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, sends):
            with pytest.raises(ValueError, match="INVALID_CAPTURE_META_EVENT") as caught:
                await window.capture_once(client, protocol)
            assert capture.diagnostic_fields(caught.value) == {
                "capture_phase": "receive",
                "capture_reason": "CAPTURE_INPUT_REJECTED",
            }
            assert window.status()["state"] == "STOPPED"
            assert window.status()["new_inputs"] == 0 and core.model.calls == sends == []

    run(scenario())


@pytest.mark.parametrize(
    "fault,phase,reason",
    [
        ("probe", "protocol_probe", "CAPTURE_CONNECTION_FAILED"),
        ("status", "protocol_probe", "CAPTURE_HTTP_STATUS_REJECTED"),
        ("auth", "ws_handshake", "CAPTURE_WS_HANDSHAKE_REJECTED"),
        ("timeout", "receive", "CAPTURE_TIMEOUT"),
        ("persist", "persist", "CAPTURE_PERSISTENCE_FAILED"),
    ],
)
def test_capture_cli_preserves_fixed_phase_without_private_exception_text(
    core, tmp_path, monkeypatch, capsys, fault, phase, reason
):
    private = "Bearer synthetic-private-secret https://private.invalid/owner/body"

    async def source(ws):
        if fault == "persist":
            await ws.send(json.dumps(raw_event()))
        await ws.wait_closed()

    def response(request):
        if fault == "probe":
            raise httpx.ConnectError(private, request=request)
        if fault == "status":
            return httpx.Response(401, text=private)

    if fault == "timeout":
        monkeypatch.setattr(capture, "CAPTURE_WAIT_SECONDS", 0.01)
    if fault == "persist":

        def refused(*args):
            raise OSError(private)

        monkeypatch.setattr(capture, "write_evidence", refused)

    async def scenario():
        async with source_window(
            core, tmp_path, source, reject_auth=fault == "auth", protocol_reply=response
        ) as (window, client, protocol, _, sends):
            with pytest.raises(Exception) as caught:
                await window.capture_once(client, protocol)
            assert capture.diagnostic_fields(caught.value) == {
                "capture_phase": phase,
                "capture_reason": reason,
            }
            assert window.db.execute("SELECT COUNT(*) FROM window_inputs").fetchone()[0] == 0
            assert core.model.calls == sends == []
            return caught.value

    error = run(scenario())
    output, value = cli_failure(monkeypatch, capsys, error)
    assert value == {
        "state": "STOPPED",
        "error": "BRIDGE_FAILED_CLOSED",
        "capture_phase": phase,
        "capture_reason": reason,
    }
    assert private not in output and "Authorization" not in output


def test_core_stop_unconfirmed_retains_original_capture_diagnostic(
    core, tmp_path, monkeypatch, capsys
):
    async def source(ws):
        await ws.close()

    async def scenario():
        async with source_window(core, tmp_path, source, live=True) as (
            window,
            client,
            protocol,
            _,
            sends,
        ):

            class LostStop(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    if request.url.path == "/internal/v1/live-text/stop":
                        raise httpx.ConnectError("private stop detail")
                    return await client._transport.handle_async_request(request)

            async with httpx.AsyncClient(
                base_url=client.base_url, headers=client.headers, transport=LostStop()
            ) as unavailable:
                with pytest.raises(ValueError, match="LIVE_TEXT_CORE_STOP_UNCONFIRMED") as caught:
                    await window.capture_once(unavailable, protocol)
            assert window.status()["state"] == "CORE_STOP_UNCONFIRMED"
            assert window.status()["new_inputs"] == 0 and core.model.calls == sends == []
            return caught.value

    error = run(scenario())
    _, value = cli_failure(monkeypatch, capsys, error)
    assert value == {
        "state": "CORE_STOP_UNCONFIRMED",
        "error": "LIVE_TEXT_CORE_STOP_UNCONFIRMED",
        "capture_phase": "receive",
        "capture_reason": "CAPTURE_WS_CLOSED",
    }


def test_unknown_or_malformed_diagnostic_never_exports_exception_arguments(monkeypatch, capsys):
    error = ValueError({"url": "private://secret"})
    error.capture_diagnostic = capture.CaptureDiagnostic(["private://secret"], "bad")
    output, value = cli_failure(monkeypatch, capsys, error)
    assert value == {"state": "STOPPED", "error": "BRIDGE_FAILED_CLOSED"}
    assert "secret" not in output
    assert capture.capture_diagnostic(error, "admission").reason == "CAPTURE_CHECK_REJECTED"


def test_daemon_task_group_retains_only_safe_leaf_diagnostic(monkeypatch, capsys):
    async def failed_transfer():
        error = httpx.ReadTimeout("https://private.example/image?secret=token")
        error.capture_diagnostic = capture.capture_diagnostic(error, "image_download")
        raise error

    async def daemon():
        async with asyncio.TaskGroup() as group:
            group.create_task(failed_transfer())

    with pytest.raises(ExceptionGroup) as caught:
        run(daemon())
    nested = ExceptionGroup("private outer detail", [
        ValueError("private unrelated detail"), caught.value,
    ])
    output, value = cli_failure(monkeypatch, capsys, nested)
    assert value == {
        "state": "STOPPED", "error": "BRIDGE_FAILED_CLOSED",
        "capture_phase": "image_download", "capture_reason": "CAPTURE_TIMEOUT",
    }
    assert "private" not in output and "secret" not in output
    assert capture.diagnostic_fields(ExceptionGroup("private", [ValueError("secret")])) == {}


@pytest.mark.parametrize("fresh", [True, False])
def test_delayed_owner_message_waits_independently_of_thirty_second_freshness(
    core, tmp_path, monkeypatch, fresh
):
    receiving = asyncio.Event()
    elapsed = [0]
    original_receive = capture.receive_frame

    async def receive(*args):
        receiving.set()
        return await original_receive(*args)

    monkeypatch.setattr(capture, "receive_frame", receive)
    monkeypatch.setattr(capture, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    observed = []
    original_timeout = asyncio.timeout

    def timeout(delay):
        observed.append(delay)
        return original_timeout(delay)

    monkeypatch.setattr(capture.asyncio, "timeout", timeout)

    async def source(ws):
        await receiving.wait()
        core.clock.advance(60)
        elapsed[0] += 60
        stamp = int(core.clock.now.timestamp()) - (0 if fresh else 31)
        await ws.send(json.dumps(raw_event(time=stamp)))
        await ws.wait_closed()

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, sends):
            if fresh:
                assert (await window.capture_once(client, protocol))["new_inputs"] == 1
            else:
                with pytest.raises(ValueError, match="WINDOW_FRESH_ORDINARY_OWNER_TEXT_REQUIRED"):
                    await window.capture_once(client, protocol)
                assert window.status()["state"] == "STOPPED"
            assert capture.CAPTURE_WAIT_SECONDS == 90 and capture.MAX_AGE_SECONDS == 30
            assert 90 in observed and core.model.calls == sends == []

    run(scenario())
