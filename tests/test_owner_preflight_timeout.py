"""A lost Core pre-send response must not strand a reply that never reached OneBot."""

import json

import httpx
import pytest
from pydantic import SecretStr

from companion.transport import recover_deliveries
from tests.conftest import run
from tests.native_continuous_support import frame, host
from tests.test_bridge_owner_resolution import child
from tests.test_evaluation_process import process_config


@pytest.mark.parametrize("stage", ["begin-send", "send-check"])
@pytest.mark.parametrize("error_type", [
    httpx.ReadTimeout, httpx.ReadError, httpx.WriteTimeout, httpx.RemoteProtocolError,
])
def test_lost_preflight_response_closes_without_provider_io_and_next_input_works(
    core, tmp_path, stage, error_type,
):
    number = 0

    async def source(ws):
        nonlocal number
        number += 1
        await ws.send(frame(number))
        await ws.wait_closed()

    async def scenario():
        async with host(core, tmp_path, source, ticking=False,
                        core_options={"lease_seconds": 25, "timeout_seconds": 5}) as (
            active, _, sends, _,
        ):
            core.settings.control_token = SecretStr("synthetic-owner-control-token")
            config = tmp_path / "worker.json"
            process_config(core, config)

            async def work():
                assert await child("tests.owner_runtime_process", "--config", str(config),
                    "--now", core.clock(), "--reply", "合成回复") == {
                        "worked": True, "calls": 1}

            await active.capture()
            await active.flush()
            await work()
            usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
            lost, proofs = [], []

            async def lose_response(response):
                if response.request.url.path.endswith("/" + stage) and not lost:
                    assert response.status_code == 200
                    lost.append(response.request.url.path)
                    # The real Core committed; only its HTTP response is lost.
                    core.clock.advance(core.settings.lease_seconds + 1)
                    with core.store.transaction():
                        recover_deliveries(core.store, core.clock())
                    raise error_type("synthetic lost Core ACK", request=response.request)
                if response.request.url.path.endswith("/expired-preflight"):
                    assert response.status_code == 200
                    proofs.append(json.loads(response.request.content))

            active.client.event_hooks["response"].append(lose_response)
            assert await active.pump()
            assert len(lost) == len(proofs) == 1
            assert proofs[0]["stage"] == stage
            assert not sends and active.attempts() == {"NOT_SENT": 1}
            assert core.store.rows("SELECT state,attempt_count FROM outbox") == [
                {"state": "CANCELLED", "attempt_count": 1}]
            assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
            assert active.runtime.row()["state"] == "ACTIVE"
            active.restart()
            assert active.runtime.row()["state"] == "ACTIVE"
            assert not await active.pump() and not sends
            await active.capture(count=2)
            await active.flush()
            await work()
            assert await active.pump()
            assert len(sends) == 1 and active.attempts() == {"NOT_SENT": 1, "ACK": 1}
            assert len(core.store.rows("SELECT * FROM model_usage")) == 2

    run(scenario())
