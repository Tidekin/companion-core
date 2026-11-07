"""Public SnowLuma-shaped synthetic notices; never raw QQ frame replay."""

import asyncio
import json

import pytest
from websockets.exceptions import ConnectionClosed

from companion_onebot_bridge import capture
from companion_onebot_bridge.window import request_stop
from tests.conftest import run
from tests.support.capture_diagnostics import lifecycle_frames
from tests.support.onebot_capture import bootstrap_frames, source_window
from tests.support.onebot_events import raw_event


def input_status(**changes):
    # Fixed SnowLuma db142db convertFriendInputStatus + notice envelope.
    return {
        "time": raw_event()["time"],
        "self_id": 10002,
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "input_status",
        "user_id": 10001,
        "event_type": 1,
        "status_text": "synthetic typing status",
        **changes,
    }


@pytest.mark.parametrize("event_type", [0, 1, 3])
def test_owner_typing_notices_before_text_do_not_become_captured_inputs(core, tmp_path, event_type):
    async def source(ws):
        for frame in (
            lifecycle_frames() + [bootstrap_frames()[2]] + [input_status(event_type=event_type)] * 3
        ):
            await ws.send(json.dumps(frame))
        await ws.send(json.dumps(raw_event()))
        await ws.wait_closed()

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, sends):
            result = await window.capture_once(client, protocol)
            assert result["state"] == "OPEN" and result["new_inputs"] == 1
            assert window.db.execute("SELECT COUNT(*) FROM window_inputs").fetchone()[0] == 1
            rows, _ = window.read_journal()
            assert sum(row["kind"] == "CAPTURED" for row in rows) == 1
            assert not window.db.execute("SELECT 1 FROM window_attempts").fetchone()
            evidence = list((window.spool.directory / "captures").glob("*.json"))
            assert len(evidence) == 1
            assert (
                json.loads(json.loads(evidence[0].read_text())["frame"])["post_type"] == "message"
            )
            assert "synthetic typing status" not in evidence[0].read_text()
            assert core.model.calls == sends == []
            assert not core.store.one("SELECT 1 FROM events")

    run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        {"self_id": 10003},
        {"user_id": 10003},
        {"user_id": True},
        {"user_id": None},
        {"group_id": 0},
        {"sender": {"user_id": 10001, "group_id": 1}},
        {"notice_type": "friend_add"},
        {"sub_type": "poke"},
        {"unknown": "synthetic"},
        {"event_type": "1"},
        {"event_type": True},
        {"event_type": 1.0},
        {"status_text": None},
        {"status_text": []},
        {"status_text": "x" * 257},
        {"status_text": "bad\x00status"},
        {"time": raw_event()["time"] - 31},
        {"time": raw_event()["time"] + 1},
        {"time": str(raw_event()["time"])},
    ],
)
def test_unverified_or_malformed_notice_stops_before_following_text(core, tmp_path, change):
    async def source(ws):
        await ws.send(json.dumps(input_status(**change)))
        await ws.send(json.dumps(raw_event()))
        await ws.wait_closed()

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, sends):
            with pytest.raises(ValueError):
                await window.capture_once(client, protocol)
            assert window.status()["state"] == "STOPPED" and window.status()["new_inputs"] == 0
            assert not window.db.execute("SELECT 1 FROM window_inputs").fetchone()
            assert core.model.calls == sends == []
            assert not core.store.one("SELECT 1 FROM events")

    run(scenario())


@pytest.mark.parametrize("limit", ["frames", "bytes", "deadline", "stop", "window"])
def test_typing_notices_share_existing_receive_bounds_and_stop(core, tmp_path, monkeypatch, limit):
    seen = asyncio.Event()
    original = capture.validate_input_status

    def validate(window, raw):
        original(window, raw)
        if limit == "stop":
            request_stop(window.spool.directory)
        if limit == "window":
            core.clock.now = window.plan.end
        seen.set()

    monkeypatch.setattr(capture, "validate_input_status", validate)

    async def source(ws):
        try:
            if limit == "bytes":
                heartbeat = bootstrap_frames()[2]
                heartbeat["status"]["padding"] = "x" * 65000
                for frame in (heartbeat, input_status(), heartbeat, input_status(), heartbeat):
                    await ws.send(json.dumps(frame))
            elif limit == "stop":
                await ws.send(json.dumps(input_status()))
                await ws.wait_closed()
            elif limit == "window":
                await ws.send(json.dumps(input_status()))
                await seen.wait()
                await ws.send(json.dumps(raw_event()))
                await ws.wait_closed()
            else:
                for _ in range(17):
                    await ws.send(json.dumps(input_status()))
                    if limit == "deadline":
                        await asyncio.sleep(0.06)
        except ConnectionClosed:
            pass

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, sends):
            if limit == "deadline":
                monkeypatch.setattr(capture, "CAPTURE_WAIT_SECONDS", 0.15)
                with pytest.raises(TimeoutError):
                    await window.capture_once(client, protocol)
            else:
                code = {
                    "frames": "CAPTURE_FRAME_CAP",
                    "bytes": "CAPTURE_BYTE_CAP",
                    "stop": "WINDOW_STOPPED",
                    "window": "WINDOW_OUTSIDE_INTERVAL",
                }[limit]
                with pytest.raises(ValueError, match=code):
                    await window.capture_once(client, protocol)
            assert seen.is_set()
            assert window.status()["state"] == "STOPPED" and window.status()["new_inputs"] == 0
            assert core.model.calls == sends == []
            assert not core.store.one("SELECT 1 FROM events")

    run(scenario())


def test_owner_notice_never_approves_a_following_third_party_message(core, tmp_path):
    async def source(ws):
        await ws.send(json.dumps(input_status()))
        await ws.send(json.dumps(raw_event(user_id=10003, sender={"user_id": 10003})))
        await ws.wait_closed()

    async def scenario():
        async with source_window(core, tmp_path, source) as (window, client, protocol, _, sends):
            with pytest.raises(ValueError, match="WINDOW_OWNER_TEXT_INPUT_REQUIRED"):
                await window.capture_once(client, protocol)
            assert window.status()["state"] == "STOPPED" and window.status()["new_inputs"] == 0
            assert core.model.calls == sends == [] and not core.store.one("SELECT 1 FROM events")

    run(scenario())
