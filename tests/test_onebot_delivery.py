
import httpx
import pytest

from companion.api import create_app
from companion.api_store import ApiStore
from companion.transport import reconcile_delivery, recover_deliveries
from companion_onebot_bridge.onebot_client import OneBotClient
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.onebot import provider
from tests.support.onebot_delivery import core_client
from tests.support.onebot_events import config, normalized


@pytest.mark.parametrize(
    "receipt,outcome",
    [
        (None, "SENT"),
        (httpx.ReadTimeout("synthetic"), "DELIVERY_UNCERTAIN"),
        ({"status": "failed", "retcode": 1200, "data": None}, "DELIVERY_UNCERTAIN"),
        ({"status": "ok", "retcode": 0, "data": {}}, "DELIVERY_UNCERTAIN"),
        ({"status": "ok", "retcode": False, "data": {"message_id": 4}}, "DELIVERY_UNCERTAIN"),
    ],
    ids=["accepted", "timeout", "failed", "missing-id", "invalid-retcode"],
)
def test_actual_core_send_state_machine_never_replays_after_io(core, tmp_path, receipt, outcome):
    sends = []
    cfg = config(data_dir=str(tmp_path / "bridge"), delivery_enabled=True)

    async def scenario():
        with Spool(cfg) as spool:
            spool.put(normalized())
            async with (
                core_client(core) as http,
                httpx.AsyncClient(
                    base_url="http://onebot",
                    transport=httpx.MockTransport(lambda r: provider(r, sends, receipt)),
                ) as protocol,
            ):
                assert await spool.flush_one(http)
                assert await core.run_once("offline")
                onebot = OneBotClient(cfg, protocol)
                await deliver_tick(http, onebot, spool)
                await deliver_tick(http, onebot, spool)
                core.clock.advance(130)
                await deliver_tick(http, onebot, spool)

    run(scenario())
    assert len(sends) == 1
    row = core.store.one("SELECT * FROM outbox")
    assert row["state"] == outcome
    assert row["provider_message_id"] == ("123456" if outcome == "SENT" else None)
    assert row["attempt_count"] == 1


@pytest.mark.parametrize("fault", ["before-begin", "after-begin", "after-result"])
def test_lost_core_ack_never_reissues_onebot_send(core, tmp_path, fault):
    sends = []
    cfg = config(data_dir=str(tmp_path / "bridge"), delivery_enabled=True)
    app = create_app(core.settings)
    app.state.core = core
    app.state.api_store = ApiStore(core.database, core.settings.state_directory)
    inner = httpx.ASGITransport(app=app)
    tripped = False

    class LoseAck(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal tripped
            begin = request.url.path.endswith("/begin-send")
            result = request.url.path.endswith("/result")
            if not tripped and (
                (begin and fault != "after-result") or (result and fault == "after-result")
            ):
                tripped = True
                if fault != "before-begin":
                    await inner.handle_async_request(request)
                raise httpx.ReadTimeout("SYNTHETIC_LOST_ACK")
            return await inner.handle_async_request(request)

    async def scenario():
        with Spool(cfg) as spool:
            spool.put(normalized())
            async with (
                core_client(core, LoseAck()) as http,
                httpx.AsyncClient(
                    base_url="http://onebot",
                    transport=httpx.MockTransport(lambda r: provider(r, sends)),
                ) as protocol,
            ):
                await spool.flush_one(http)
                await core.run_once("offline")
                with pytest.raises(httpx.ReadTimeout):
                    await deliver_tick(http, OneBotClient(cfg, protocol), spool)
        # Restart both the bridge's durable state and its transport clients.
        with Spool(cfg) as spool:
            async with (
                core_client(core) as http,
                httpx.AsyncClient(
                    base_url="http://onebot",
                    transport=httpx.MockTransport(lambda r: provider(r, sends)),
                ) as protocol,
            ):
                core.clock.advance(130)
                await deliver_tick(http, OneBotClient(cfg, protocol), spool)
                await deliver_tick(http, OneBotClient(cfg, protocol), spool)

    run(scenario())
    assert len(sends) == (0 if fault == "after-begin" else 1)
    row = core.store.one("SELECT * FROM outbox")
    assert row["state"] == ("DELIVERY_UNCERTAIN" if fault == "after-begin" else "SENT")


def test_restart_after_durable_send_intent_reports_uncertain_without_send(core, tmp_path):
    cfg = config(data_dir=str(tmp_path / "bridge"), delivery_enabled=True)
    sends = []

    async def scenario():
        async with core_client(core) as http:
            with Spool(cfg) as spool:
                spool.put(normalized())
                await spool.flush_one(http)
                await core.run_once("offline")
                item = (
                    await http.post(
                        "/internal/v1/deliveries/lease",
                        json={
                            "worker_id": "crash-fixture",
                            "limit": 1,
                        },
                    )
                ).json()["items"][0]
                await http.post(
                    f"/internal/v1/deliveries/{item['message_id']}/begin-send",
                    json={"lease_token": item["lease_token"]},
                )
                spool.start_delivery(item)
            with Spool(cfg) as spool:
                async with httpx.AsyncClient(
                    base_url="http://onebot",
                    transport=httpx.MockTransport(lambda r: provider(r, sends)),
                ) as protocol:
                    await deliver_tick(http, OneBotClient(cfg, protocol), spool)

    run(scenario())
    assert sends == []
    assert core.store.one("SELECT state FROM outbox")["state"] == "DELIVERY_UNCERTAIN"


@pytest.mark.parametrize("resolution", ["CANCELLED", "SENT"])
def test_late_provider_receipt_cannot_override_human_reconciliation(core, tmp_path, resolution):
    cfg = config(data_dir=str(tmp_path / "bridge"), delivery_enabled=True)
    sends = []

    async def scenario():
        with Spool(cfg) as spool:
            spool.put(normalized())
            async with core_client(core) as http:
                await spool.flush_one(http)
                await core.run_once("offline-late-ack")
                item = (await http.post(
                    "/internal/v1/deliveries/lease", json={"worker_id": "offline", "limit": 1}
                )).json()["items"][0]
                started = await http.post(
                    f"/internal/v1/deliveries/{item['message_id']}/begin-send",
                    json={"lease_token": item["lease_token"]},
                )
                started.raise_for_status()
                spool.start_delivery(item)
                spool.finish_delivery(item["message_id"], "123456")
                core.clock.advance(130)
                with core.store.transaction():
                    recover_deliveries(core.store, core.clock())
                reconcile_delivery(core.store, item["message_id"], resolution, core.clock())
                assert await spool.flush_result(http)
                assert spool.db.execute("SELECT state FROM results").fetchone()[0] == "rejected"
                async with httpx.AsyncClient(
                    base_url="http://onebot",
                    transport=httpx.MockTransport(lambda r: provider(r, sends)),
                ) as protocol:
                    assert not await deliver_tick(http, OneBotClient(cfg, protocol), spool)

    run(scenario())
    assert core.store.one("SELECT state FROM outbox")["state"] == resolution
    assert sends == []


@pytest.mark.parametrize("mutation", ["owner", "action", "content", "account", "disabled"])
def test_unsafe_delivery_cannot_start_external_send(core, tmp_path, mutation):
    sends = []
    cfg = config(data_dir=str(tmp_path / "bridge"), delivery_enabled=mutation != "disabled")

    def protocol_response(request):
        if mutation == "account" and request.url.path == "/get_login_info":
            return httpx.Response(
                200, json={"status": "ok", "retcode": 0, "data": {"user_id": 99999}}
            )
        return provider(request, sends)

    async def scenario():
        with Spool(cfg) as spool:
            spool.put(normalized())
            async with (
                core_client(core) as http,
                httpx.AsyncClient(
                    base_url="http://onebot", transport=httpx.MockTransport(protocol_response)
                ) as protocol,
            ):
                await spool.flush_one(http)
                await core.run_once("offline")
                if mutation == "owner":
                    core.store.execute("UPDATE conversations SET platform_session_key='bad'")
                if mutation == "action":
                    core.settings.proactive_enabled = True
                    core.settings.quiet_start, core.settings.quiet_end = 0, 0
                    core.store.execute("UPDATE outbox SET action_type='qq.private.proactive'")
                if mutation == "content":
                    core.store.execute('UPDATE outbox SET content_json=%s', ('{"image":"bad"}',))
                if mutation != "disabled":
                    with pytest.raises(ValueError):
                        await deliver_tick(http, OneBotClient(cfg, protocol), spool)
                else:
                    assert not await deliver_tick(http, OneBotClient(cfg, protocol), spool)

    run(scenario())
    assert sends == []
    assert core.store.one("SELECT state FROM outbox")["state"] in {"PENDING", "LEASED"}
