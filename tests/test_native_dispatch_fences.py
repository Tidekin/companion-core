"""Native equivalents of the historical synthetic-window transport fence tests."""

import json
from datetime import datetime

import httpx
import pytest

from tests.conftest import run
from tests.support.native_dispatch import attempts, dispatch, staged


@pytest.mark.parametrize("path", ["/get_login_info", "/get_status", "/get_version_info"])
@pytest.mark.parametrize("change", ["deadline", "stop"])
def test_native_probe_rechecks_after_dns_wait(core, tmp_path, path, change):
    async def scenario():
        async with staged(core, tmp_path) as (owner, client, protocol, guard, sends):
            dispatched, changed, reservations = [], [], []
            original = protocol._transport
            reserve = guard.reserve

            def observed_reserve(item):
                reservations.append(core.clock.now)
                return reserve(item)

            guard.reserve = observed_reserve

            class Observed(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    dispatched.append(request.url.path)
                    return await original.handle_async_request(request)

                async def aclose(self):
                    await original.aclose()

            protocol._transport = Observed()
            async def after_dns(request):
                if request.url.path == path:
                    assert attempts(owner) == {} and guard.current_item is None
                    if change == "deadline":
                        lease = core.store.one("SELECT lease_until FROM outbox")["lease_until"]
                        deadline = datetime.fromisoformat(lease)
                        assert core.clock.now < deadline
                        core.clock.now = deadline
                    else:
                        owner.stop()
                    changed.append(core.clock.now)
            protocol.event_hooks["request"].append(after_dns)
            with pytest.raises(ValueError, match=("^OWNER_DELIVERY_LEASE_LOST$"
                if change == "deadline" else "^OWNER_RUNTIME_STOPPED$")):
                await dispatch(owner, client, protocol, guard)
            assert len(changed) == 1 and not sends and not guard.dispatched
            if change == "deadline":
                assert core.clock.now == changed[0] and reservations == changed
                assert set(dispatched) == {"/get_login_info", "/get_status", "/get_version_info"}
            else:
                assert path not in dispatched and reservations == []
            assert attempts(owner) == {} and guard.current_item is None
            assert core.store.one("SELECT attempt_count FROM outbox")["attempt_count"] == 0
            assert not owner.db.execute("SELECT 1 FROM results").fetchone()
    run(scenario())


@pytest.mark.parametrize("change", ["text", "message_id", "lease_token", "delivery_key"])
def test_native_item_and_wire_mutation_keep_original_reservation(core, tmp_path, change):
    async def scenario():
        async with staged(core, tmp_path) as (owner, client, protocol, guard, sends):
            original = core.store.one("SELECT message_id,content_sha256 FROM outbox")
            async def mutate(request):
                if request.url.path != "/send_private_msg":
                    return
                item = guard.current_item
                if change == "text":
                    item["content"]["text"] = "changed text"
                    body = json.loads(request.content)
                    body["message"][0]["data"]["text"] = "changed text"
                    request._content = json.dumps(body).encode()
                    request.stream = httpx.ByteStream(request.content)
                    request.headers["Content-Length"] = str(len(request.content))
                else:
                    item[change] = {"message_id": "msg_" + "e" * 32,
                                    "lease_token": "lease_" + "e" * 32,
                                    "delivery_key": "reply:evt_" + "e" * 64 + ":v1"}[change]
            protocol.event_hooks["request"].append(mutate)
            with pytest.raises(ValueError, match="OWNER_DELIVERY_BINDING_CHANGED"):
                await dispatch(owner, client, protocol, guard)
            assert not sends and not guard.dispatched
            attempt = owner.db.execute(
                "SELECT message_id,text_hash,state FROM window_attempts").fetchone()
            assert attempt == (original["message_id"], original["content_sha256"], "RESERVED")
            # A corrupted item cannot replace the original uncertain spool receipt.
            receipt = json.loads(owner.db.execute("SELECT body FROM results").fetchone()[0])
            assert receipt["result"] == "DELIVERY_UNCERTAIN"
    run(scenario())


@pytest.mark.parametrize("change", ["stream", "transfer_encoding", "unbounded_stream"])
def test_native_request_stream_uses_checked_bytes(core, tmp_path, change):
    async def scenario():
        async with staged(core, tmp_path) as (owner, client, protocol, guard, sends):
            async def replace_stream(request):
                if request.url.path != "/send_private_msg":
                    return
                if change == "transfer_encoding":
                    request.headers["Transfer-Encoding"] = "chunked"
                elif change == "unbounded_stream":
                    class UnknownStream(httpx.AsyncByteStream):
                        async def __aiter__(self):
                            pytest.fail("unknown stream must not be consumed")
                            yield b"unreachable"
                    request.stream = UnknownStream()
                else:
                    request.stream = httpx.ByteStream(b'{"changed":true}')
                    request.headers["Content-Length"] = "16"
            protocol.event_hooks["request"].append(replace_stream)
            await dispatch(owner, client, protocol, guard)
            if change == "transfer_encoding":
                assert not sends and attempts(owner) == {"UNKNOWN": 1}
            else:
                assert len(sends) == 1 and attempts(owner) == {"ACK": 1}
                assert sends[0]["message"] == [{"type": "text", "data": {"text": "合成回答"}}]
    run(scenario())


@pytest.mark.parametrize("phase", ["capture", "send"])
def test_native_probe_transmits_the_checked_empty_body(core, tmp_path, phase):
    async def scenario():
        async with staged(core, tmp_path) as (owner, client, protocol, guard, sends):
            received = []
            original = protocol._transport
            class Observed(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    received.append((request.url.path, await request.aread()))
                    return await original.handle_async_request(request)

                async def aclose(self):
                    await original.aclose()
            protocol._transport = Observed()
            async def replace_stream(request):
                if request.url.path != "/send_private_msg":
                    request.stream = httpx.ByteStream(b'{"changed":true}')
                    request.headers["Content-Length"] = "16"
            protocol.event_hooks["request"].append(replace_stream)
            if phase == "capture":
                assert (await owner.probe(client, protocol))["connection_ready"]
            else:
                await dispatch(owner, client, protocol, guard)
                assert attempts(owner) == {"ACK": 1}
            probes = [body for path, body in received if path != "/send_private_msg"]
            assert len(probes) == 3 and probes == [b"{}"] * 3
    run(scenario())


def test_ready_probe_is_reused_briefly_for_the_next_segment(core, tmp_path, monkeypatch):
    from companion_onebot_bridge import owner_send
    from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard

    async def scenario():
        async with staged(core, tmp_path) as (owner, client, protocol, guard, sends):
            probes = []

            async def count(request):
                if request.url.path == "/get_status":
                    probes.append(request.url.path)

            protocol.event_hooks["request"].append(count)
            assert await dispatch(owner, client, protocol, guard)
            assert len(sends) == 1 and len(probes) == 1

            async def probe():
                return await OwnerOneBot(OwnerSendGuard(owner, client), protocol).probe()

            assert (await probe())["connection_ready"] and len(probes) == 1
            now = owner_send.monotonic()
            monkeypatch.setattr(owner_send, "monotonic",
                                lambda: now + owner_send.PROBE_REUSE_SECONDS + 1)
            assert (await probe())["connection_ready"] and len(probes) == 2
            # A stopped runtime is never answered from the reused probe.
            owner.stop()
            with pytest.raises(ValueError, match="^OWNER_RUNTIME_STOPPED$"):
                await probe()
            assert len(probes) == 2
    run(scenario())
