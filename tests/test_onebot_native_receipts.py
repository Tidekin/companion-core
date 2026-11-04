import json
from dataclasses import replace

import httpx
import pytest

from companion_onebot_bridge.native_actions import NativeAck, content_hash, prepare
from companion_onebot_bridge.onebot_client import OneBotClient
from companion_onebot_bridge.outbound import validate_item
from companion_onebot_bridge.spool import Spool
from tests.conftest import run
from tests.support.onebot_events import config


def item():
    return {"message_id": "msg_" + "a" * 32, "lease_token": "lease_" + "b" * 32,
            "lease_until": "2026-10-24T10:00:00+00:00",
            "conversation_platform_session_key": config().origin,
            "action_type": "qq.private.poke", "content": {"kind": "poke",
                "target_user_id": config().owner_qq_id},
            "delivery_key": "poke-response:job_" + "c" * 32,
            "window_origin_event_id": "evt_" + "d" * 64}


def test_native_success_retains_actual_ack_without_fabricating_message_id(tmp_path):
    requests, response = [], {"status": "ok", "retcode": 0, "data": None}
    cfg = replace(config(), delivery_enabled=True, data_dir=str(tmp_path / "spool"))

    async def scenario():
        def reply(request):
            requests.append(request)
            return httpx.Response(200, json=response)

        async with httpx.AsyncClient(base_url=cfg.onebot_http_url,
                                     transport=httpx.MockTransport(reply)) as client:
            receipt = await OneBotClient(cfg, client).send_native(prepare(item()["content"],
                                                                        cfg.owner_qq_id))
            assert isinstance(receipt, NativeAck)
            assert receipt.response_sha256 == content_hash(response)
            with Spool(cfg) as spool:
                spool.start_delivery(item())
                spool.finish_delivery(item()["message_id"], receipt)
                result = json.loads(spool.db.execute("SELECT body FROM results").fetchone()[0])
                assert result == {"lease_token": item()["lease_token"], "result": "SENT",
                                  "provider_message_id": None, "native_ack": receipt.value()}
        assert len(requests) == 1 and requests[0].url.path == "/friend_poke"
        assert requests[0].content == prepare(item()["content"], cfg.owner_qq_id).body
        assert json.loads(requests[0].content) == {"user_id": 10001, "target_id": 10001}

    run(scenario())


@pytest.mark.parametrize("response", [
    {"status": "ok", "retcode": 0}, {"status": "ok", "retcode": 0, "data": {}},
    {"status": "ok", "retcode": False, "data": None},
    {"status": "failed", "retcode": 0, "data": None},
    {"status": "ok", "retcode": 1, "data": None},
    {"status": "ok", "retcode": 0, "data": {"message_id": 1}},
])
def test_native_bad_receipt_never_counts_as_acceptance(response):
    cfg = replace(config(), delivery_enabled=True)

    async def scenario():
        async with httpx.AsyncClient(base_url=cfg.onebot_http_url,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))) as client:
            with pytest.raises(ValueError, match="INVALID_NATIVE_ACK"):
                await OneBotClient(cfg, client).send_native(prepare(item()["content"],
                                                                   cfg.owner_qq_id))

    run(scenario())


@pytest.mark.parametrize("change", [
    {"content": {"kind": "poke", "target_user_id": "10003"}},
    {"content": {"kind": "poke", "target_user_id": "10001", "group_id": "90001"}},
    {"delivery_key": "reply:evt_" + "d" * 64 + ":v1"}, {"reply_revision": 1},
    {"reply_segment": {}}, {"window_notice": "commitment"},
])
def test_native_envelope_is_finite_and_owner_bound(change):
    validate_item(item(), config(), companion=True)
    with pytest.raises(ValueError):
        validate_item(item() | change, config(), companion=True)
    with pytest.raises(ValueError):
        validate_item(item(), config())


def test_native_permission_is_closed_and_prepared_bytes_cannot_change():
    async def scenario():
        calls = []
        async with httpx.AsyncClient(base_url=config().onebot_http_url,
            transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
            original = prepare(item()["content"], config().owner_qq_id)
            with pytest.raises(ValueError, match="DELIVERY_DISABLED"):
                await OneBotClient(config(), client).send_native(original)
            with pytest.raises(ValueError, match="PRIVATE_OWNER_POKE_REQUIRED"):
                await OneBotClient(replace(config(), delivery_enabled=True), client).send_native(
                    replace(original, body=b'{"user_id":10003,"target_id":10003}'))
            assert not calls

    run(scenario())
