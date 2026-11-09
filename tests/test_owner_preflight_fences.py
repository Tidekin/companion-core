"""Real native WS/HTTP/worker boundaries around an unreserved health check."""

from types import SimpleNamespace

import httpx
import pytest

from companion.transport import recover_deliveries
from companion_onebot_bridge import owner_preflight
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.spool import encode
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, worker_for
from tests.support.onebot_events import raw_event
from tests.support.owner_preflight_fences import ready


@pytest.mark.parametrize("fault", ["transport", "http_status"])
def test_protocol_health_failure_leaves_no_attempt_and_can_retry(core, tmp_path, fault):
    failed, calls = False, []

    def response(request):
        nonlocal failed
        calls.append(request.url.path)
        if not failed and request.url.path == "/get_login_info":
            failed = True
            if fault == "transport":
                raise httpx.ConnectError("synthetic protocol transport failure", request=request)
            return httpx.Response(503)

    async def scenario():
        async with ready(core, tmp_path, protocol_reply=response) as (active, sends, _):
            expected = httpx.ConnectError if fault == "transport" else httpx.HTTPStatusError
            with pytest.raises(expected):
                await active.pump()
            assert not active.attempts() and not sends
            assert not active.db.execute("SELECT 1 FROM results").fetchone()
            assert core.store.one("SELECT attempt_count FROM outbox")["attempt_count"] == 0
            active.restart()
            assert active.runtime.row()["state"] == "ACTIVE"
            core.clock.advance(core.settings.lease_seconds + 1)
            calls.clear()
            assert await active.pump()
            assert sorted(calls) == ["/get_login_info", "/get_status", "/get_version_info",
                                     "/send_private_msg"]
            assert len(sends) == 1 and active.attempts() == {"ACK": 1}
            assert core.store.one("SELECT state FROM outbox")["state"] == "SENT"

    run(scenario())


@pytest.mark.parametrize("condition", ["offline", "identity", "version"])
def test_unready_health_remains_fatal_without_a_reservation(core, tmp_path, condition):
    def response(request):
        if condition == "offline" and request.url.path == "/get_status":
            data = {"online": False, "good": False}
        elif condition == "identity" and request.url.path == "/get_login_info":
            data = {"user_id": 10003}
        elif condition == "version" and request.url.path == "/get_version_info":
            data = {"app_name": "SnowLuma", "app_version": "wrong-version"}
        else:
            return None
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})

    async def scenario():
        async with ready(core, tmp_path, protocol_reply=response) as (active, sends, _):
            with pytest.raises(ValueError, match="^ONEBOT_NOT_READY$"):
                await active.pump()
            assert not sends and not active.attempts()
            assert core.store.one("SELECT attempt_count FROM outbox")["attempt_count"] == 0
            assert not active.db.execute("SELECT 1 FROM results").fetchone()
            active.restart()
            assert active.runtime.row()["state"] == "ACTIVE"
            assert not active.runtime.stop_path.exists()

    run(scenario())


@pytest.mark.parametrize("boundary", ["stop", "lease_expiry"])
def test_state_changes_during_health_check_fence_reservation(core, tmp_path, boundary):
    async def scenario():
        async with ready(core, tmp_path) as (active, sends, _):
            async def change(response):
                if response.request.url.path == "/get_version_info":
                    if boundary == "stop":
                        active.runtime.stop()
                    else:
                        core.clock.advance(core.settings.lease_seconds + 1)

            active.protocol.event_hooks["response"].append(change)
            with pytest.raises(ValueError, match=("^OWNER_RUNTIME_STOPPED$" if boundary == "stop"
                                                  else "^OWNER_DELIVERY_LEASE_LOST$")):
                await active.pump()
            assert not sends and not active.attempts()
            assert core.store.one("SELECT attempt_count FROM outbox")["attempt_count"] == 0
            active.runtime.verify(active=False)
            assert active.runtime.stop_path.exists() == (boundary == "stop")

    run(scenario())


def test_new_input_after_probe_keeps_final_send_fence_and_next_turn_progress(core, tmp_path):
    async def scenario():
        async with ready(core, tmp_path) as (active, sends, usage):
            interrupted = False

            async def new_input(response):
                nonlocal interrupted
                if response.request.url.path.endswith("/begin-send") and not interrupted:
                    interrupted = True
                    raw = encode(raw_event(2, time=int(core.clock.now.timestamp()),
                                           text="synthetic newer input"))
                    event = normalize(raw, active.config, now=core.clock.now.timestamp())
                    assert active.runtime.persist_capture(event, raw, received_at=core.clock.now,
                                                          peer=("127.0.0.1", 3001))

            active.client.event_hooks["response"].append(new_input)
            assert await active.pump()
            assert interrupted and not sends and active.attempts() == {"NOT_SENT": 1}
            assert core.store.one("SELECT state,last_error_code FROM outbox") == {
                "state": "CANCELLED", "last_error_code": "REPLY_NOT_DISPATCHED"}
            assert active.runtime.row()["state"] == "ACTIVE"
            await active.flush()
            next_model = Provider(answer("synthetic next turn"))
            async with worker_for(core, next_model) as worker:
                assert await worker.run_once("next-preflight-fence-worker")
            assert len(next_model.requests) == 1
            assert await active.pump()
            assert len(sends) == 1 and active.attempts() == {"ACK": 1, "NOT_SENT": 1}
            assert core.store.rows('SELECT state FROM outbox ORDER BY record_sequence') == [
                {"state": "CANCELLED"}, {"state": "SENT"}]
            assert core.store.rows(
                'SELECT * FROM model_usage ORDER BY record_sequence'
            )[:1] == usage
            active.runtime.verify()

    run(scenario())


def test_begin_send_lost_ack_waits_for_lease_before_signed_settlement(core, tmp_path, monkeypatch):
    async def scenario():
        async with ready(core, tmp_path) as (active, sends, _):
            failed = False
            waits = []

            async def expire(seconds):
                waits.append(seconds)
                assert seconds > 0 and not sends
                assert core.store.one("SELECT state,attempt_count FROM outbox") == {
                    "state": "SENDING", "attempt_count": 1}
                core.clock.advance(seconds + 1)
                recover_deliveries(core.store, core.clock())

            monkeypatch.setattr(owner_preflight, "asyncio", SimpleNamespace(sleep=expire))

            async def lose_ack(response):
                nonlocal failed
                if response.request.url.path.endswith("/begin-send") and not failed:
                    failed = True
                    await response.aread()
                    raise httpx.ReadTimeout("synthetic committed begin-send ACK lost",
                                            request=response.request)

            active.client.event_hooks["response"].append(lose_ack)
            assert await active.pump()
            assert len(waits) == 1
            assert not sends and active.attempts() == {"NOT_SENT": 1}
            assert core.store.one("SELECT state,attempt_count FROM outbox") == {
                "state": "CANCELLED", "attempt_count": 1}
            active.restart()
            assert active.runtime.row()["state"] == "ACTIVE"
            assert not await active.pump()
            assert not sends and active.attempts() == {"NOT_SENT": 1}

    run(scenario())
