"""The explicit command grammar and bounded ordinary-text path share one ingress."""

import json

import pytest

from companion_onebot_bridge.spool import Spool
from companion_onebot_bridge.window import TestWindow
from tests.conftest import run
from tests.support.live_text_window import (
    live_clients,
    live_fixture,
    native_admit,
    native_clients,
    native_send,
)
from tests.support.onebot_events import raw_event
from tests.support.onebot_window import input_approval


@pytest.mark.parametrize("text", ["暂停写日记", "暂停主动分享", "记住这个：我喜欢绿茶"])
def test_command_words_are_ordinary_live_text_without_control_side_effects(core, tmp_path, text):
    async def scenario():
        async with native_clients(core, tmp_path) as ctx:
            await native_admit(ctx, text=text)
            assert await ctx.worker.run_once()
            assert await native_send(ctx)
            assert len(ctx.calls) == len(ctx.sends) == 1
            assert ctx.spool.db.execute("SELECT state FROM window_attempts").fetchone()[0] == "ACK"
            body = json.loads(core.store.one("SELECT body_json FROM events")["body_json"])
            assert not body.get("diary_command") and not body.get("control_command")
            assert not core.store.rows(
                "SELECT * FROM runtime_meta WHERE key IN "
                "('diary_paused','proactive_paused','life_paused')")
            assert not core.store.rows("SELECT * FROM memories")
    run(scenario())


@pytest.mark.parametrize("text", ["/暂停写日记", " \n/暂停主动分享\t", "/记住这个：我喜欢绿茶"])
def test_explicit_controls_are_rejected_before_bounded_live_work(core, tmp_path, text):
    cfg, plan = live_fixture(core, tmp_path)

    async def scenario():
        async with live_clients(core, cfg) as (client, _, model_calls, sends):
            with Spool(cfg) as spool:
                window = TestWindow(spool, plan, clock=lambda: core.clock.now)
                await window.open(client)
                raw = raw_event(1, text=text)
                with pytest.raises(
                    ValueError, match="LIVE_TEXT_ORDINARY_AUTHENTICATED_INPUT_REQUIRED"
                ):
                    await window.ingest(client, raw, input_approval(plan, raw))
                assert model_calls == sends == []
                assert not core.store.rows("SELECT * FROM jobs")
                assert not core.store.rows("SELECT * FROM outbox")
                assert not core.store.rows("SELECT * FROM model_usage")

    run(scenario())
