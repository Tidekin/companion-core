"""Owner messages sent while the Bridge was restarting are read back once, not lost."""

import asyncio
import json
from datetime import timedelta

import pytest

from companion_onebot_bridge.backfill import backfill
from companion_onebot_bridge.onebot_client import OneBotClient
from tests.conftest import run
from tests.support import onebot
from tests.support.native_consumer_entry import environment
from tests.support.onebot_events import raw_event


@pytest.fixture(autouse=True)
def empty_history():
    onebot.HISTORY.clear()
    yield
    onebot.HISTORY.clear()


def history(number, text, at):
    frame = raw_event(number, text=text, time=int(at))
    return {key: value for key, value in frame.items() if key not in {"post_type", "self_id"}}


async def added(entry, protocol):
    return (await backfill(entry.runtime, entry.client, protocol, asyncio.Event()))["added"]


def kinds(entry):
    return [json.loads(row[0])["kind"] for row in entry.db.execute(
        "SELECT capture_json FROM window_inputs ORDER BY rowid")]


def test_a_missed_owner_message_is_captured_once_and_accepted_by_core(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path) as (entry, _):
            # The messages arrive after the authority was activated, as in a Bridge restart.
            core.clock.now += timedelta(seconds=90)
            now = core.clock.now.timestamp()
            bot = history(2, "机器人自己发的", now - 3)
            bot["user_id"] = bot["sender"]["user_id"] = 10002
            onebot.HISTORY.extend([history(1, "停顿时发的", now - 5), bot,
                                   history(3, "一分钟前的", now - 60)])
            protocol = OneBotClient(entry.config, entry.protocol)
            assert await added(entry, protocol) == 1
            assert kinds(entry) == ["onebot_history_backfill"]
            assert await entry.runtime.flush_one(entry.client)
            row = entry.db.execute("SELECT event_id FROM window_inputs").fetchone()
            assert (await entry.state({"event_id": row[0]}))["state"] == "PENDING"
            assert await added(entry, protocol) == 0
            assert kinds(entry) == ["onebot_history_backfill"]

    run(scenario())


def test_messages_before_the_last_live_capture_are_not_read_back(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path) as (entry, _):
            core.clock.now += timedelta(seconds=30)
            await entry.capture(5, "实时收到的")
            core.clock.now += timedelta(seconds=2)
            onebot.HISTORY.append(history(4, "更早的", core.clock.now.timestamp() - 10))
            protocol = OneBotClient(entry.config, entry.protocol)
            assert await added(entry, protocol) == 0
            assert kinds(entry) == ["authenticated_onebot_ws"]

    run(scenario())


def test_messages_before_the_bridge_bound_the_authority_are_skipped(core, tmp_path):
    async def scenario():
        async with environment(core, tmp_path) as (entry, _):
            # Core refuses captures older than the authority; uploading one would stop
            # the Bridge, so a message from before the binding is left alone.
            onebot.HISTORY.append(history(6, "绑定之前", core.clock.now.timestamp() - 5))
            protocol = OneBotClient(entry.config, entry.protocol)
            assert await added(entry, protocol) == 0
            assert kinds(entry) == []

    run(scenario())
