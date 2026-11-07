"""A failed connection before the Core POST can recover without duplicating any send."""

import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from companion.config import SegmentedReplyConfig
from companion_onebot_bridge import owner_preflight
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.spool import encode
from tests.conftest import run
from tests.native_continuous_support import frame, host
from tests.support.bridge_owner_resolution import child
from tests.support.onebot_events import raw_event
from tests.support.owner_preflight_fences import ready
from tests.test_evaluation_process import process_config


@pytest.mark.parametrize("stage", ["begin-send", "send-check"])
@pytest.mark.parametrize("error_type", [
    httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
])
def test_connection_failure_before_core_dispatch_retries_the_same_bound_preflight(
    core, tmp_path, stage, error_type,
):
    async def source(ws):
        await ws.send(frame(1))
        await ws.wait_closed()

    async def scenario():
        policy = SegmentedReplyConfig(max_segments=3, mode="semantic", interval_ms=500)
        async with host(core, tmp_path, source, policy=policy, ticking=False,
                        core_options={"lease_seconds": 25, "timeout_seconds": 5}) as (
            active, _, sends, _,
        ):
            core.settings.control_token = SecretStr("synthetic-owner-control-token")
            config = tmp_path / "worker.json"
            process_config(core, config)
            await active.capture()
            await active.flush()
            result = await child("tests.owner_runtime_process", "--config", str(config),
                "--now", core.clock(), "--reply", "合成第一段\n合成第二段\n合成第三段")
            assert result == {"worked": True, "calls": 1}
            usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
            attempts, successful = [], []
            first = None

            def identity(request):
                if not request.url.path.endswith("/" + stage):
                    return None
                return request.url.path, json.loads(request.content)

            async def disconnect(request):
                nonlocal first
                value = identity(request)
                if value is None:
                    return
                if first is None:
                    first = value
                if value == first:
                    attempts.append(value)
                    if len(attempts) == 1:
                        # Request hooks precede the real HTTP transport: Core has
                        # received no POST and cannot have committed this attempt.
                        raise error_type("synthetic pre-connect failure", request=request)

            async def received(response):
                if first is not None and identity(response.request) == first:
                    successful.append(response.status_code)

            active.client.event_hooks["request"].append(disconnect)
            active.client.event_hooks["response"].append(received)
            for _ in range(3):
                assert await active.pump()
                core.clock.advance(1)
            assert attempts == [first, first] and successful == [200]
            assert len(sends) == 3 and active.attempts() == {"ACK": 3}
            rows = core.store.rows("SELECT state,attempt_count FROM outbox ORDER BY segment_index")
            assert rows == [{"state": "SENT", "attempt_count": 1}] * 3
            assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
            assert not core.store.one("SELECT 1 FROM runtime_meta "
                                      "WHERE key LIKE 'input_cancel_before_send:%'")

    run(scenario())


@pytest.mark.parametrize("stage", ["begin-send", "send-check"])
@pytest.mark.parametrize("boundary", ["lease_expiry", "stop", "new_input"])
def test_preconnect_retry_stops_at_the_original_lease_and_live_local_fences(
    core, tmp_path, monkeypatch, stage, boundary,
):
    async def scenario():
        async with ready(core, tmp_path) as (active, sends, usage):
            requests, waits = [], []

            async def fail(request):
                if request.url.path.endswith("/" + stage):
                    requests.append(request.url.path)
                    raise httpx.ConnectError("synthetic unavailable connection", request=request)

            async def change(seconds):
                waits.append(seconds)
                if boundary == "lease_expiry":
                    core.clock.advance(core.settings.lease_seconds + 1)
                elif boundary == "stop":
                    active.runtime.stop()
                else:
                    raw = encode(raw_event(2, time=int(core.clock.now.timestamp()),
                                           text="synthetic newer input"))
                    event = normalize(raw, active.config, now=core.clock.now.timestamp())
                    assert active.runtime.persist_capture(event, raw, received_at=core.clock.now,
                                                          peer=("127.0.0.1", 3001))

            monkeypatch.setattr(owner_preflight, "asyncio", SimpleNamespace(sleep=change))
            active.client.event_hooks["request"].append(fail)
            try:
                assert await active.pump()
            except ValueError as error:
                assert boundary != "lease_expiry"
                assert str(error) in {"OWNER_RUNTIME_STOPPED", "OWNER_INPUT_PENDING"}
            assert len(waits) == len(requests) == 1
            assert 0 < waits[0] <= 0.1 and not sends
            assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
            if boundary == "lease_expiry":
                assert active.attempts() == {"NOT_SENT": 1}
                assert core.store.one("SELECT state FROM outbox")["state"] == "CANCELLED"
            active.runtime.verify(active=False)

    run(scenario())
