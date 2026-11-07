"""Actual delivery lifecycle finally clears RAM, preserving original native custody."""

import asyncio
import json

import httpx
import pytest

from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.outbound import deliver_item
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from tests.conftest import run
from tests.support.meme_core_transport import handler
from tests.support.meme_core_transport import transport_case as transport_case
from tests.support.onebot import provider


@pytest.mark.parametrize("phase", ["reserve_stop", "probe_stop", "cancelled", "begin_stop",
                                  "begin_cancelled", "sent", "unknown"])
def test_prepared_original_is_released_on_every_delivery_exit(transport_case, monkeypatch, phase):
    native, runtime, item, value, _ = transport_case
    sends, prepared, assets = [], [], []
    core_handler = handler(native, value)

    def core_reply(request):
        path = request.url.path
        if path.endswith("/cancel-before-send"):
            # This unit response cannot establish an original Core no-I/O proof.
            return httpx.Response(409, json={"error": "SOURCE_PROOF_REQUIRED"})
        if path.endswith("/begin-send"):
            assert prepared == [item["message_id"]]
            assert native.spool.db.execute(
                "SELECT state FROM window_attempts").fetchall() == [("RESERVED",)]
            if phase == "begin_stop":
                runtime.window.stop()
                runtime.window.verify()
            if phase == "begin_cancelled":
                raise asyncio.CancelledError("synthetic prepared cancellation")
            return httpx.Response(200, json={"state": "SENDING"})
        if path.endswith("/send-check"):
            return httpx.Response(200, json={"approved": True, "lease_until": item["lease_until"]})
        if path.endswith("/result"):
            return httpx.Response(200, json={"state": json.loads(request.content)["result"]})
        if path.endswith("/asset"):
            assets.append(path)
        return core_handler(request)

    def onebot_reply(request):
        if request.url.path == "/get_login_info":
            if phase == "probe_stop":
                runtime.window.stop()
            if phase == "cancelled":
                raise asyncio.CancelledError("synthetic probe cancellation")
        if request.url.path == "/send_private_msg" and phase == "unknown":
            sends.append(json.loads(request.content))
            raise httpx.ReadTimeout("synthetic lost ACK", request=request)
        return provider(request, sends)

    async def scenario():
        # The original signed frame is admitted through the actual API. Only this
        # isolated delivery endpoint/protocol transport is substituted for faults.
        async with native.client(runtime) as real_client:
            assert await runtime.window.flush_one(real_client)
        async with (
            private_http_client(
                runtime.config.core_url,
                runtime.config.bridge_token,
                transport=httpx.MockTransport(core_reply),
            ) as client,
            private_http_client(
                runtime.config.onebot_http_url,
                runtime.config.onebot_token,
                transport=httpx.MockTransport(onebot_reply),
            ) as protocol,
        ):
            guard = OwnerSendGuard(runtime.window, client)
            onebot = OwnerOneBot(guard, protocol)
            prepare = onebot.prepare_delivery

            async def observe_prepared(current):
                await prepare(current)
                assert guard.prepared_message is not None
                assert guard.prepared_message[1] == {"type": "image", "data": {
                    "file": "base64://" + value["data_base64"]}}
                prepared.append(current["message_id"])
                if phase == "reserve_stop":
                    runtime.window.stop()

            monkeypatch.setattr(onebot, "prepare_delivery", observe_prepared)
            if phase in {"reserve_stop", "probe_stop", "begin_stop"}:
                with pytest.raises(ValueError, match="OWNER_RUNTIME_STOPPED"):
                    await deliver_item(client, onebot, native.spool, item, guard=guard)
            elif phase in {"cancelled", "begin_cancelled"}:
                with pytest.raises(asyncio.CancelledError, match=("synthetic probe cancellation"
                    if phase == "cancelled" else "synthetic prepared cancellation")):
                    await deliver_item(client, onebot, native.spool, item, guard=guard)
            else:
                assert await deliver_item(client, onebot, native.spool, item, guard=guard)
            assert guard.prepared_message is None
            pin = native.spool.db.execute("SELECT state FROM meme_pins").fetchone()
            attempts = native.spool.db.execute("SELECT state FROM window_attempts").fetchall()
            if phase in {"probe_stop", "cancelled"}:
                assert not prepared and not assets and pin is None
                assert attempts == [] and sends == []
                assert not native.spool.db.execute("SELECT 1 FROM results").fetchone()
                assert not native.spool.db.execute("SELECT 1 FROM meme_runtime_bindings").fetchone()
                return
            assert prepared == [item["message_id"]] and len(assets) == 1
            assert pin is not None
            pin = pin[0]
            if phase == "reserve_stop":
                assert pin == "PREPARED" and attempts == [] and sends == []
            elif phase in {"begin_stop", "begin_cancelled"}:
                assert pin == "PREPARED" and attempts == [("RESERVED",)] and sends == []
                assert not native.spool.db.execute("SELECT 1 FROM results").fetchone()
            elif phase == "sent":
                assert pin == "SENT" and attempts == [("ACK",)] and len(sends) == 1
            else:
                assert pin == "UNKNOWN" and attempts == [("UNKNOWN",)] and len(sends) == 1
                result = native.spool.db.execute("SELECT body FROM results").fetchone()[0]
                assert json.loads(result)["result"] == "DELIVERY_UNCERTAIN"

    run(scenario())
