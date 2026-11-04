"""Whole native reply allowance is checked without reserving future attempts."""

import json
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import pytest
from anyio import Path

from companion import cumulative, owner_delivery
from companion.core import Core
from companion.reply_segments import SegmentedReplyConfig
from tests.conftest import run
from tests.native_continuous_support import answer, frame, host
from tests.test_cumulative_budget import policy
from tests.test_paid_model_gate import approved_live


@asynccontextmanager
async def group(core, tmp_path, period, allowance):
    async def source(ws):
        await ws.send(frame(1, text="请分两段说明。"))
        await ws.wait_closed()

    protocol_calls = []

    def protocol(request):
        protocol_calls.append(request.url.path)

    approved_live(core, tmp_path, max_output_tokens=4096, per_attempt_micro_usd=100000,
                  per_request_micro_usd=200000)
    scope = policy(core, **{period: {"send_attempts": allowance}})
    async with host(core, tmp_path, source,
        policy=SegmentedReplyConfig(min_chars=4, max_segments=2, interval_ms=0),
        paid_options={"cumulative": scope}, protocol_reply=protocol,
        model_reply=lambda request: answer("第一段独立回复。\n第二段独立回复。")) as (
        bridge, _, sends, calls,
    ):
        await bridge.capture()
        # Approval follows receipt. The fixture clock can lag the WebSocket wall clock.
        capture, = bridge.db.execute("SELECT capture_json FROM window_inputs").fetchone()
        received_at = datetime.fromisoformat(json.loads(capture)["received_at"])
        core.clock.now = max(core.clock.now, received_at)
        await bridge.flush()
        with Core(core.settings, core.model, core.clock) as worker:
            assert worker.store is not core.store
            assert await worker.run_once("group-quota-independent")
        rows = core.store.rows("SELECT * FROM outbox ORDER BY segment_index")
        assert len(rows) == 2 and all(row["segment_count"] == 2 for row in rows)
        assert len(calls) == 1
        yield bridge, sends, protocol_calls, rows


def send_operations(core):
    state = cumulative.check(core.store, core.settings, core.clock())
    return {key: value for key, value in state["operations"].items() if key.startswith("send:")}


@pytest.mark.parametrize("period", ["window", "day", "month", "account"])
def test_first_fragment_requires_allowance_for_entire_native_group(core, tmp_path, period):
    async def scenario():
        async with group(core, tmp_path, period, 1) as (bridge, sends, protocol_calls, rows):
            usage = core.store.rows("SELECT * FROM model_usage")
            journal = Path(core.settings.paid_model.journal_path)
            before = (await journal.read_bytes())
            for _ in range(2):
                with pytest.raises(httpx.HTTPStatusError) as error:
                    await bridge.pump()
                assert error.value.response.status_code == 429
            assert sends == protocol_calls == []
            assert bridge.attempts() == {} and send_operations(core) == {}
            assert core.store.rows("SELECT * FROM outbox ORDER BY segment_index") == rows
            assert core.store.rows("SELECT * FROM model_usage") == usage
            assert (await journal.read_bytes()) == before
    run(scenario())


@pytest.mark.parametrize("period", ["window", "day", "month", "account"])
def test_exact_group_quota_reuses_ack_prefix_after_restart_and_idempotent_check(
    core, tmp_path, period,
):
    async def scenario():
        async with group(core, tmp_path, period, 2) as (bridge, sends, _, rows):
            journal = Path(core.settings.paid_model.journal_path)
            usage = core.store.rows("SELECT * FROM model_usage")
            replaying, repeated = False, []

            async def replay(response):
                nonlocal replaying
                if not response.request.url.path.endswith("/send-check") or replaying:
                    return
                await response.aread()
                assert response.status_code == 200, response.text
                body = json.loads(response.request.content)
                replaying = True
                try:
                    before = (await journal.read_bytes())
                    again = await bridge.client.post(response.request.url.path, json=body)
                    assert again.status_code == 200, again.text
                    assert again.json() == response.json()
                    assert (await journal.read_bytes()) == before
                    repeated.append(body["message_id"])
                finally:
                    replaying = False

            bridge.client.event_hooks["response"].append(replay)
            before = (await journal.read_bytes())
            with core.store.transaction():
                owner_delivery.check_delivery(core.store, core.settings, core.clock,
                                              rows[0]["message_id"])
            assert send_operations(core) == {} and (await journal.read_bytes()) == before
            assert await bridge.pump()
            assert len(sends) == 1 and len(send_operations(core)) == 1
            prefix = bridge.runtime.verify()
            bridge.restart()
            assert bridge.attempts() == {"ACK": 1}
            before = (await journal.read_bytes())
            with Core(core.settings, core.model, core.clock) as reader:
                with reader.store.transaction():
                    owner_delivery.check_delivery(reader.store, reader.settings, reader.clock,
                                                  rows[1]["message_id"])
            assert len(send_operations(core)) == 1 and (await journal.read_bytes()) == before
            assert await bridge.pump()
            assert len(sends) == 2 and bridge.attempts() == {"ACK": 2}
            assert bridge.runtime.verify()[:len(prefix)] == prefix
            assert repeated == [row["message_id"] for row in rows]
            assert len(send_operations(core)) == 2
            assert all(value["state"] == "SENT" for value in send_operations(core).values())
            assert cumulative.status(core.store, core.settings, core.clock())["used"][period][
                "send_attempts"] == 2
            assert core.store.rows("SELECT * FROM model_usage") == usage
    run(scenario())
