"""Native health failures cannot acquire a durable send reservation."""

import json

import httpx
import pytest

from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, worker_for
from tests.support.onebot import provider
from tests.support.prepare_owner_input import native_spool, window
from tests.test_native_image_pipeline import admit_image


def test_preflight_failure_does_not_poison_retry_or_restart(core, tmp_path):
    async def scenario():
        async with window(core, tmp_path) as (_, config, plan), private_http_client(
            config.core_url, config.bridge_token,
        ) as client:
            await admit_image(core, client, config, plan, tmp_path, message=[
                {"type": "text", "data": {"text": "synthetic health check"}}])
            model = Provider(answer("synthetic reply"))
            async with worker_for(core, model) as worker:
                assert await worker.run_once("preflight-reservation-worker")
            usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
            assert len(model.requests) == len(usage) == 1 and usage[0]["state"] == "SETTLED"
            fail = True
            cancellations, sends = [], []

            async def fail_core_check(request):
                nonlocal fail
                if fail and request.url.path == "/internal/v1/owner-authority/check":
                    fail = False
                    raise httpx.ConnectError("synthetic Core check failure", request=request)

            async def observe(response):
                if response.request.url.path.endswith("/cancel-before-send"):
                    await response.aread()
                    cancellations.append((response.status_code, response.json()))

            client.event_hooks["request"].append(fail_core_check)
            client.event_hooks["response"].append(observe)
            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                async with private_http_client(config.onebot_http_url, config.onebot_token,
                    transport=httpx.MockTransport(lambda request: provider(request, sends)),
                ) as protocol:
                    async def pump():
                        guard = OwnerSendGuard(runtime, client)
                        return await deliver_tick(client, OwnerOneBot(guard, protocol),
                                                  spool, guard=guard)

                    with pytest.raises(httpx.ConnectError):
                        await pump()
                    attempts_after_failure = [record["kind"] for record in runtime.verify()
                                              if record["kind"] == "RESERVED"]
                    assert not sends
                    assert core.store.one("SELECT attempt_count FROM outbox")["attempt_count"] == 0
                    assert not spool.db.execute("SELECT 1 FROM results").fetchone()
                    # Use native lease recovery and a fresh dispatch operation, not
                    # altered reservations, journals, locks, or a model rerun.
                    core.clock.advance(core.settings.lease_seconds + 1)
                    retry_error = None
                    try:
                        assert await pump()
                    except ValueError as error:
                        retry_error = error.args
            finally:
                spool.close()
            reopened, recovered = native_spool(core, tmp_path, config, plan)
            try:
                outcome = {"first_reservations": attempts_after_failure,
                           "retry_error": retry_error, "restart_state": recovered.row()["state"]}
                assert outcome == {"first_reservations": [], "retry_error": None,
                                   "restart_state": "ACTIVE"}, outcome
                assert not recovered.stop_path.exists() and not cancellations
                assert len(sends) == 1
                assert core.store.one("SELECT state,attempt_count FROM outbox") == {
                    "state": "SENT", "attempt_count": 1}
                body, state = reopened.db.execute("SELECT body,state FROM results").fetchone()
                assert state == "accepted" and json.loads(body)["result"] == "SENT"
                assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
                recovered.verify()
            finally:
                reopened.close()

    run(scenario())
