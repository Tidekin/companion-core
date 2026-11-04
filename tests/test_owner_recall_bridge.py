"""Canonical notice identities and durable authenticated recall delivery."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from companion_onebot_bridge import continuous, owner_notices
from companion_onebot_bridge.event_normalizer import normalize, normalize_recall
from tests.conftest import run
from tests.native_continuous_support import frame, host
from tests.native_continuous_support import recall_frame as native_recall_frame
from tests.support.onebot_events import config, raw_event
from tests.test_onebot_capture import source_window
from tests.test_private_companion import companion_options


def recall_frame(message_id=1, **changes):
    return json.dumps({"time": raw_event()["time"], "self_id": 10002, "post_type": "notice",
                       "notice_type": "friend_recall", "user_id": 10001,
                       "message_id": message_id, **changes})


def test_recall_uses_canonical_inbound_id_and_isolates_protocol_epoch():
    cfg = config()
    incoming = normalize(json.dumps(raw_event(-123)), cfg)
    recall = normalize_recall(recall_frame(-123), cfg)
    assert recall["target_event_id"] == incoming["event_id"]
    assert recall["target_source_event_id"] == incoming["source_event_id"]
    assert normalize_recall(recall_frame("-123"), cfg) == recall
    assert normalize_recall(recall_frame(-123), replace(cfg, protocol_epoch="another-epoch"))[
        "target_event_id"] != recall["target_event_id"]
    assert normalize_recall(recall_frame(-123, user_id=10003), cfg) is None
    assert normalize_recall(recall_frame(-123, self_id=10003), cfg) is None


@pytest.mark.parametrize("change", [{"message_id": 0}, {"group_id": 10004},
                                    {"message_id": True}, {"time": True}])
def test_recall_rejects_noncanonical_notice(change):
    with pytest.raises(ValueError):
        normalize_recall(recall_frame(**change), config())


def test_pending_recall_is_signed_persistent_and_survives_window_stop(core, tmp_path):
    async def source(ws):
        await ws.wait_closed()

    async def scenario():
        async with source_window(core, tmp_path, source, live=True, approved=True,
                                 scope_options=companion_options(core)) as (
            window, client, protocol, _, sends,
        ):
            receiver = continuous.OwnerSource(window, protocol, asyncio.Event())
            receiver.put_recall(recall_frame(), core.clock.now, ("127.0.0.1", 12345))
            receiver.put_recall(recall_frame(), core.clock.now, ("127.0.0.1", 12345))
            row = window.db.execute("SELECT * FROM automatic_recalls").fetchone()
            assert row["state"] == "PENDING"
            receipt = json.loads(row["record_json"])
            assert continuous.verify_recall(window, receipt) == receipt
            tampered = receipt | {"target_event_id": "evt_" + "0" * 64}
            with pytest.raises(ValueError, match="AUTHENTICATED_RECALL_REQUIRED"):
                continuous.verify_recall(window, tampered)
            resumed = continuous.OwnerSource(window, protocol, asyncio.Event())
            resumed.put_recall(recall_frame(), core.clock.now, ("127.0.0.1", 12345))
            assert window.db.execute("SELECT COUNT(*) FROM automatic_recalls").fetchone()[0] == 1
            await window.stop_live(client)
            assert window.db.execute("SELECT COUNT(*) FROM automatic_recalls").fetchone()[0] == 1
            assert await continuous.flush_recalls(window, client)
            saved = window.db.execute("SELECT * FROM automatic_recalls").fetchone()
            assert saved["state"] == "ACCEPTED"
            assert json.loads(saved["response_json"])["state"] == "PENDING_TARGET"
            assert not await continuous.flush_recalls(window, client)
            assert window.status()["state"] == "STOPPED" and sends == []
            assert core.store.rows("SELECT * FROM model_usage") == []

    run(scenario())


def test_notice_before_inbound_prevents_generation_and_does_not_stop_window(core, tmp_path):
    async def source(ws):
        await ws.send(native_recall_frame())
        await ws.send(native_recall_frame())
        await ws.send(frame(1, text="这段内容已经撤回"))
        await ws.send(frame(2, text="保留的新消息"))
        await ws.wait_closed()

    async def scenario():
        async with host(core, tmp_path, source) as (active, _, sends, calls):
            await active.capture()
            recalled = normalize_recall(native_recall_frame(), active.config)["target_event_id"]
            assert active.db.execute("SELECT 1 FROM window_inputs WHERE event_id=?",
                                     (recalled,)).fetchone() is None
            assert active.db.execute("SELECT COUNT(*) FROM automatic_recalls").fetchone()[0] == 1
            assert active.runtime.row()["state"] == "ACTIVE"
            assert active.attempts() == {} and sends == calls == []
            assert core.store.rows("SELECT * FROM jobs") == []
            assert core.store.rows("SELECT * FROM model_usage") == []
            active.runtime.verify()

    run(scenario())


@pytest.mark.parametrize("arrival", ["before_frame", "before_reference", "after_reference"])
def test_once_capture_routes_recall_and_skips_withdrawn_reference_io(core, tmp_path, arrival):
    runtime = None
    reference_reads = []

    async def source(ws):
        if arrival == "before_frame":
            await ws.send(native_recall_frame())
        await ws.send(frame(1, message=[
            {"type": "text", "data": {"text": "撤回的引用"}},
            {"type": "reply", "data": {"id": "42"}},
        ]))
        await ws.send(frame(2, text="仍然有效的输入"))
        await ws.wait_closed()

    def recall():
        owner_notices.put(runtime, native_recall_frame(), datetime.now(UTC), ("127.0.0.1", 12345))

    def protocol_reply(request):
        if request.url.path in {"/get_msg", "/get_forward_msg"}:
            reference_reads.append(request)
            assert arrival == "after_reference"
            recall()
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {
                "message_id": 42, "user_id": 10001, "message_type": "private",
                "message": [{"type": "text", "data": {"text": "已读取的引用"}}]}})

    async def before(request):
        if arrival == "before_reference" and request.url.path == "/get_msg":
            recall()

    async def scenario():
        nonlocal runtime
        async with host(core, tmp_path, source, protocol_reply=protocol_reply,
                        bridge_options={"rich_input_scope": "references_v1"}) as (
            active, _, sends, calls,
        ):
            runtime = active.runtime
            active.protocol.event_hooks["request"].append(before)
            await active.capture()
            assert await owner_notices.flush_one(active.runtime, active.client)
            recalled = normalize_recall(native_recall_frame(), active.config)["target_event_id"]
            assert active.db.execute("SELECT 1 FROM window_inputs WHERE event_id=?",
                                     (recalled,)).fetchone() is None
            assert len(reference_reads) == (1 if arrival == "after_reference" else 0)
            assert active.captures() == 1
            assert active.runtime.row()["state"] == "ACTIVE" and sends == calls == []
            assert core.store.rows("SELECT * FROM jobs") == []
            assert core.store.rows("SELECT * FROM model_usage") == []
            active.runtime.verify()

    run(scenario())


@pytest.mark.parametrize("phase", ["before_delegate", "after_send_check", "after_delegate"])
def test_recall_dispatch_boundary_distinguishes_not_sent_from_unknown(core, tmp_path, phase):
    runtime = None
    invoked = []

    async def source(ws):
        await ws.send(frame(1, text="待撤回的消息"))
        await ws.wait_closed()

    def recall():
        owner_notices.put(runtime, native_recall_frame(), datetime.now(UTC), ("127.0.0.1", 12345))

    def remote(request):
        if request.url.path == "/send_private_msg":
            invoked.append(request)
            if phase == "after_delegate":
                recall()
                raise httpx.ReadTimeout("synthetic post-dispatch uncertainty")

    async def scenario():
        nonlocal runtime
        async with host(core, tmp_path, source, protocol_reply=remote, independent_api=True) as (
            active, _, sends, calls,
        ):
            runtime = active.runtime
            await active.capture()
            await active.flush()
            assert await core.run_once()

            async def before(request):
                if request.url.path == "/send_private_msg" and phase == "before_delegate":
                    recall()

            async def after_check(response):
                if (phase == "after_send_check"
                    and response.request.url.path.endswith("/send-check")):
                    recall()

            active.protocol.event_hooks["request"].append(before)
            active.client.event_hooks["response"].append(after_check)
            assert await active.pump()
            if phase != "after_delegate":
                assert runtime.row()["state"] == "ACTIVE"
                assert active.attempts() == {"NOT_SENT": 1}
                assert sends == invoked == []
                assert core.store.one("SELECT state FROM outbox")["state"] == "CANCELLED"
                assert json.loads(active.db.execute("SELECT body FROM results").fetchone()[0])[
                    "result"] == "NOT_SENT"
                assert active.captures() == 1
            else:
                assert runtime.row()["state"] == "STOPPED"
                assert active.attempts() == {"UNKNOWN": 1}
                assert len(invoked) == 1 and sends == []
                assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"
                with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                    await active.pump()
                assert len(invoked) == 1
            runtime.verify(active=False)
            assert len(calls) == 1
            assert core.store.one("SELECT state FROM model_usage")["state"] == "SETTLED"

    run(scenario())
