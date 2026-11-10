"""Both pre-dispatch checks contend on native control and ledger locks over HTTP."""

import json

import httpx
import psycopg
import pytest
from pydantic import SecretStr

from companion.config import SegmentedReplyConfig
from companion.postgres import BUDGET_LEDGER, ERASURE_LEDGER
from companion.transport import recover_deliveries
from companion_delivery_protocol.preflight import BUSY
from companion_onebot_bridge.owner_runtime import OwnerRuntime
from tests.conftest import run
from tests.native_continuous_support import frame, host
from tests.support.bridge_owner_resolution import child
from tests.support.evaluation_process import process_config


@pytest.mark.parametrize("stage", ["begin-send", "send-check"])
@pytest.mark.parametrize("expire", [False, True])
@pytest.mark.parametrize("lock_domain", ["control", "budget", "erasure"])
def test_busy_preflight_preserves_single_dispatch_and_next_input(
    core, tmp_path, stage, expire, lock_domain,
):
    number = 0

    async def source(ws):
        nonlocal number
        number += 1
        await ws.send(frame(number))
        await ws.wait_closed()

    async def scenario():
        policy = SegmentedReplyConfig(max_segments=3, mode="semantic", interval_ms=500)
        async with host(
            core,
            tmp_path,
            source,
            policy=policy,
            ticking=False,
            core_options={"lease_seconds": 25, "timeout_seconds": 5},
        ) as (
            active,
            _,
            sends,
            _,
        ):
            config = tmp_path / "worker.json"
            core.settings.control_token = SecretStr("synthetic-owner-control-token")
            process_config(core, config)

            async def run_worker():
                result = await child(
                    "tests.owner_runtime_process",
                    "--config",
                    str(config),
                    "--now",
                    core.clock(),
                    "--reply",
                    "第一段合成回复\n第二段合成回复\n第三段合成回复",
                )
                assert result == {"worked": True, "calls": 1}

            await active.capture()
            await active.flush()
            await run_worker()
            writer = psycopg.connect(core.settings.database.get_secret_value())
            calls, busy, proofs = [], [], []

            async def lock(request):
                if request.url.path.endswith("/" + stage):
                    if not calls:
                        if lock_domain == "control":
                            writer.execute(
                                "SELECT 1 FROM runtime_meta WHERE key='privacy_epoch' FOR UPDATE")
                        else:
                            writer.execute(
                                "SELECT pg_advisory_xact_lock(hashtext(current_schema()),%s)",
                                (BUDGET_LEDGER if lock_domain == "budget" else ERASURE_LEDGER,),
                            )
                    calls.append(json.loads(request.content))

            async def observe(response):
                if response.request.url.path.endswith("/" + stage) and response.status_code == 503:
                    await response.aread()
                    busy.append(response.json())
                    assert not sends
                    assert active.attempts() == {"RESERVED": 1}
                    first = core.store.one(
                        'SELECT state,attempt_count FROM outbox ORDER BY segment_index LIMIT 1')
                    assert first == {
                        "state": "LEASED" if stage == "begin-send" else "SENDING",
                        "attempt_count": int(stage == "send-check"),
                    }
                    writer.rollback()
                    if expire:
                        core.clock.advance(core.settings.lease_seconds + 1)
                        with core.store.transaction():
                            recover_deliveries(core.store, core.clock())
                if response.request.url.path.endswith("/expired-preflight") and response.is_success:
                    proofs.append(json.loads(response.request.content))
                    if len(proofs) == 1:
                        raise httpx.ReadTimeout(
                            "lost settlement response", request=response.request
                        )

            active.client.event_hooks["request"].append(lock)
            active.client.event_hooks["response"].append(observe)
            try:
                if expire:
                    with pytest.raises(httpx.ReadTimeout):
                        await active.pump()
                    assert not sends
                    active.runtime = OwnerRuntime(active.spool, clock=lambda: core.clock.now)
                    assert await active.pump()
                    assert proofs == [proofs[0]] * 2
                    forged = dict(proofs[0], reserved_record_sha256="0" * 64)
                    denied = await active.client.post(
                        "/internal/v1/owner-authority/expired-preflight", json=forged
                    )
                    assert denied.status_code == 403
                    assert active.attempts() == {"NOT_SENT": 1}
                    assert (
                        core.store.rows("SELECT state FROM outbox") == [{"state": "CANCELLED"}] * 3
                    )
                    await active.capture(count=2)
                    await active.flush()
                    await run_worker()
                for _ in range(3):
                    assert await active.pump()
                    core.clock.advance(1)
            finally:
                writer.close()
            assert busy == [{"error": BUSY}]
            if not expire:
                assert calls[0] == calls[1]
            assert len(sends) == 3
            sent = core.store.rows(
                "SELECT provider_message_id,attempt_count FROM outbox "
                "WHERE state='SENT' ORDER BY segment_index"
            )
            assert len(sent) == 3 and all(row["attempt_count"] == 1 for row in sent)
            assert active.runtime.row()["state"] == "ACTIVE"
            assert len(core.store.rows("SELECT * FROM model_usage")) == (2 if expire else 1)

    run(scenario())
