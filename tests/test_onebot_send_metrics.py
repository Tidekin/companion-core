"""Send-call diagnostics preserve delivery uncertainty and never export exception text."""

import asyncio
import gc
import json
import weakref
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from companion_onebot_bridge import metrics
from companion_onebot_bridge.outbound import _deliver_item
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.astrbot_delivery import deliver_expression, journal
from tests.support.onebot_events import config
from tests.test_onebot_rpc_metrics import CANARY, records


async def attempt(tmp_path, fault, clock=None):
    cfg = config(data_dir=str(tmp_path), delivery_enabled=True)
    item = {"message_id": "msg_" + "a" * 32, "lease_token": "lease_" + "b" * 32,
        "lease_until": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        "conversation_platform_session_key": cfg.origin, "action_type": "qq.private.reply",
        "content": {"text": CANARY}, "delivery_key": "synthetic-private-" + CANARY}
    sent, calls = [], []
    entered = asyncio.Event()
    failure = ValueError(CANARY) if fault == "validation" else httpx.ReadTimeout(CANARY)

    async def probe():
        return {"connection_ready": True}

    async def send_content(content):
        assert content == item["content"]
        if clock is not None:
            clock[0] += 5000000  # checked-transport preflight
            clock[0] += 7000000  # provider transport
        if fault != "predelegate_timeout":
            sent.append(True)
        if fault == "cancel":
            entered.set()
            await asyncio.Event().wait()
        if fault != "ack":
            raise failure
        return "synthetic-ack"

    def core(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if calls[-1] == "begin-send":
            if clock is not None:
                clock[0] += 3000000
            return httpx.Response(200, json={"state": "SENDING"})
        assert calls[-1] == "result"
        if clock is not None:
            clock[0] += 13000000
        return httpx.Response(200, json={"state": json.loads(request.content)["result"]})

    with Spool(cfg) as spool:
        async with httpx.AsyncClient(base_url="http://private.invalid",
            transport=httpx.MockTransport(core),
        ) as client:
            onebot = SimpleNamespace(config=cfg, probe=probe, send_content=send_content)
            async def cancel(item):
                clock[0] += 17000000
                return False

            guard = (SimpleNamespace(reserve=lambda item: None, cancel_before_send=cancel,
                complete=lambda item, ack: None) if clock is not None else None)
            task = asyncio.create_task(_deliver_item(client, onebot, spool, item, guard=guard))
            if fault == "cancel":
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel(CANARY)
                with pytest.raises(asyncio.CancelledError) as caught:
                    await task
                assert caught.value.args == (CANARY,) and task.cancelled()
            else:
                assert await task
        body, state = spool.db.execute("SELECT body,state FROM results").fetchone()
        result = json.loads(body)
        assert result["result"] == ("SENT" if fault == "ack" else "DELIVERY_UNCERTAIN")
        assert state == ("pending" if fault == "cancel" else "accepted")
        assert result.get("error_code") == (None if fault == "ack" else "BRIDGE_SEND_INTERRUPTED")
        assert result.get("provider_message_id") == ("synthetic-ack" if fault == "ack" else None)
        assert calls == (["begin-send"] if fault == "cancel" else ["begin-send", "result"])
        assert len(sent) == int(fault != "predelegate_timeout")
        return result, state, calls, sent


@pytest.mark.parametrize("fault,error_class", [
    ("ack", "none"), ("predelegate_timeout", "timeout"), ("lost_ack", "timeout"),
    ("validation", "validation"), ("cancel", "cancelled"),
])
def test_send_metrics_classify_original_failure_without_changing_state(
    tmp_path, monkeypatch, capsys, fault, error_class,
):
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    outcomes = []
    for enabled in (False, True):
        monkeypatch.setenv("COMPANION_TELEMETRY", "1" if enabled else "0")
        outcomes.append(run(attempt(tmp_path / str(enabled), fault)))
        rows = [row for row in records(capsys) if row["stage"] == "t_onebot_send_result"]
        assert [row["error_class"] for row in rows] == [error_class] * enabled
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("sink_failure", [OSError, ValueError, RuntimeError])
def test_send_metric_output_failure_preserves_success_uncertainty_and_cancel(
    tmp_path, monkeypatch, sink_failure,
):
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")

    def refused(*args, **kwargs):
        raise sink_failure(CANARY)

    monkeypatch.setattr(metrics, "print", refused, raising=False)
    for fault in ("ack", "predelegate_timeout", "cancel"):
        run(attempt(tmp_path / fault, fault))


def test_send_span_includes_preflight_and_provider_but_ends_before_cancellation_receipt(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("COMPANION_TELEMETRY", "1")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    clock = [1000000]
    monkeypatch.setattr(metrics.time, "perf_counter_ns", lambda: clock[0])
    run(attempt(tmp_path, "lost_ack", clock))
    rows = [row for row in records(capsys) if row["stage"] == "t_onebot_send_result"]
    assert len(rows) == 1 and rows[0]["elapsed_ms"] == 12


@pytest.mark.parametrize("enabled", [False, True])
def test_swallowed_send_timeout_releases_astrbot_spool_without_cyclic_gc(
    tmp_path, monkeypatch, capsys, enabled,
):
    monkeypatch.setenv("COMPANION_TELEMETRY", "1" if enabled else "0")
    monkeypatch.setenv("COMPANION_TELEMETRY_EVIDENCE", "OFFLINE_FIXTURE")
    item = {"message_id": "msg_" + "a" * 32, "lease_token": "lease_" + "b" * 32,
        "lease_until": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        "conversation_platform_session_key": "aiocqhttp:FriendMessage:10001",
        "action_type": "qq.private.reply", "content": {"text": CANARY},
        "delivery_key": "synthetic-lifetime"}
    spool_ref = None

    async def send(origin, chain):
        raise httpx.ReadTimeout(CANARY)

    def respond(request):
        return httpx.Response(200, json={"state": "SENDING" if request.url.path.endswith(
            "/begin-send") else "DELIVERY_UNCERTAIN"})

    async def scenario():
        nonlocal spool_ref
        spool = journal(tmp_path)
        spool_ref = weakref.ref(spool)
        async with httpx.AsyncClient(base_url="http://private.invalid",
            transport=httpx.MockTransport(respond),
        ) as client:
            return await deliver_expression(client, item, None, lambda text, image: text,
                                            send, spool=spool)

    was_enabled = gc.isenabled()
    gc.disable()
    try:
        result = run(scenario())
        assert result["result"] == "DELIVERY_UNCERTAIN"
        assert result["error_code"] == "BRIDGE_SEND_INTERRUPTED"
        assert spool_ref() is None, "Swallowed send exception retained the operation's Spool"
        reopened = journal(tmp_path)
        try:
            assert reopened.db.execute("SELECT state FROM results").fetchone()[0] == "accepted"
        finally:
            reopened.close()
    finally:
        # A failed regression still owns cleanup; release occurs after the assertion.
        if spool_ref is not None and (retained := spool_ref()) is not None:
            retained.close()
        if was_enabled:
            gc.enable()
    rows = [row for row in records(capsys) if row["stage"] == "t_onebot_send_result"]
    assert [row["error_class"] for row in rows] == ["timeout"] * enabled
