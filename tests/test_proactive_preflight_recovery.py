"""Native Life HTTP/independent worker and Bridge recover an unstarted text suffix."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr

from companion import paid_budget
from companion.owner_authority_protocol import CaptureIdentity
from companion.reply_segments import SegmentedReplyConfig
from companion.transport import recover_deliveries
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.spool import encode
from tests.conftest import run
from tests.native_continuous_support import Host
from tests.support.core_http import real_core_http
from tests.support.onebot import provider
from tests.support.onebot_events import config, raw_event
from tests.support.owner_authority import activate
from tests.test_bridge_owner_resolution import child
from tests.test_life_expression_consumption import prepared, request
from tests.test_life_expression_entrypoint import native_child
from tests.test_social_owner_authority import stop

TEXT = "合成第一段。\n\n合成第二段。\n\n合成第三段。"


@asynccontextmanager
async def ready(core, tmp_path, *, protocol_reply=None):
    core.authority_test_clients = []
    core.settings.segmented_reply = SegmentedReplyConfig(
        mode="semantic", max_segments="unlimited", interval_ms=0)
    _, policy, authority, source = prepared(core, tmp_path,
        destination="PRIVATE_MESSAGE_DRAFT", purposes=["proactive"], execute=False)
    cfg = config(data_dir=str(tmp_path / "bridge"), capture_signing_key="c" * 64,
        bridge_token=core.settings.bridge_token.get_secret_value(),
        onebot_http_url="http://127.0.0.1:3000", onebot_ws_url="ws://127.0.0.1:3001")
    stop(core, authority)
    policy = policy.model_copy(update={"purposes": [*policy.purposes, "chat", "content"],
        "capture": CaptureIdentity(public_key=cfg.capture_public_key,
            protocol_epoch=cfg.protocol_epoch,
            identity_sha256=paid_budget.sha([cfg.owner_qq_id, cfg.bot_qq_id]))})
    authority = activate(core, policy, revision=2, operation="proactive-bridge-capture")[0][
        "authority"]
    core.settings.control_token = SecretStr("synthetic-proactive-consume-token")
    sends = []

    def protocol(request):
        if protocol_reply:
            result = protocol_reply(request)
            if result is not None:
                return result
        return provider(request, sends)

    try:
        async with real_core_http(core) as port:
            cfg = replace(cfg, owner_authority=authority, core_url=f"http://127.0.0.1:{port}")
            async with private_http_client(cfg.core_url, cfg.bridge_token) as client, (
                private_http_client(cfg.onebot_http_url, cfg.onebot_token,
                    transport=httpx.MockTransport(protocol))
            ) as protocol_client:
                generated = await native_child(core, tmp_path, source,
                    "PRIVATE_MESSAGE_DRAFT", 1, wording=TEXT)
                assert generated["provider_calls"] == 1
                job = next(row for row in generated["results"] if row["state"] == "SHADOW")
                consumed = await client.post(f"/internal/v2/life/results/{job['job_id']}/consume",
                    json=request(core, job["job_id"], authority, "qq.private.proactive").model_dump(
                        mode="json"),
                    headers={"Authorization": "Bearer " +
                             core.settings.control_token.get_secret_value()})
                assert consumed.status_code == 200, consumed.text
                assert len(consumed.json()["deliveries"]) == 3
                active = Host(core, cfg, client, protocol_client)
                try:
                    yield active, sends
                finally:
                    await active.pause()
                    active.spool.close()
    finally:
        for client in core.authority_test_clients:
            await client.aclose()


@pytest.mark.parametrize("stage", ["begin-send", "send-check"])
@pytest.mark.parametrize("failure", ["connect", "before_request", "lost_ack"])
def test_proactive_suffix_preflight_failure_keeps_bridge_recoverable(
    core, tmp_path, stage, failure,
):
    async def scenario():
        async with ready(core, tmp_path) as (active, sends):
            assert await active.pump() and len(sends) == 1
            usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
            prefix = core.store.one("SELECT * FROM outbox WHERE segment_index=0")
            calls, proofs = [], []

            def expire():
                core.clock.advance(core.settings.lease_seconds + 1)
                with core.store.transaction():
                    recover_deliveries(core.store, core.clock())

            async def before(request):
                if request.url.path.endswith("/" + stage):
                    calls.append(json.loads(request.content))
                    if len(calls) == 1 and failure != "lost_ack":
                        if failure == "connect":
                            raise httpx.ConnectError("synthetic before connection", request=request)
                        expire()
                        raise httpx.RemoteProtocolError("synthetic unknown response",
                                                        request=request)

            async def after(response):
                if response.request.url.path.endswith("/" + stage) and failure == "lost_ack":
                    assert response.status_code == 200
                    expire()
                    raise httpx.ReadError("synthetic committed response loss",
                                          request=response.request)
                if response.request.url.path.endswith("/expired-preflight"):
                    assert response.status_code == 200
                    proofs.append(json.loads(response.request.content))

            active.client.event_hooks["request"].append(before)
            active.client.event_hooks["response"].append(after)
            assert await active.pump()
            if failure == "connect":
                assert calls == [calls[0], calls[0]]
                active.client.event_hooks["request"].remove(before)
                assert await active.pump()
                assert len(sends) == 3 and active.attempts() == {"ACK": 3}
                assert not proofs
            else:
                assert len(calls) == len(proofs) == 1
                assert proofs[0]["stage"] == stage
                assert len(sends) == 1 and active.attempts() == {"ACK": 1, "NOT_SENT": 1}
                rows = core.store.rows(
                    "SELECT state,attempt_count FROM outbox ORDER BY segment_index")
                assert rows == [{"state": "SENT", "attempt_count": 1},
                    {"state": "CANCELLED", "attempt_count": int(
                        stage == "send-check" or failure == "lost_ack")},
                    {"state": "CANCELLED", "attempt_count": 0}]
                assert not await active.pump()
            assert core.store.one("SELECT * FROM outbox WHERE segment_index=0") == prefix
            assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
            active.restart()
            assert active.runtime.row()["state"] == "ACTIVE"
            assert not active.runtime.stop_path.exists()
            assert not await active.pump()

    run(scenario())


def test_proactive_cancellation_lost_ack_resumes_and_next_owner_turn_works(core, tmp_path):
    async def scenario():
        async with ready(core, tmp_path) as (active, sends):
            assert await active.pump() and len(sends) == 1
            usage = core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
            proofs = []

            async def lose_begin(request):
                if request.url.path.endswith("/begin-send"):
                    core.clock.advance(core.settings.lease_seconds + 1)
                    with core.store.transaction():
                        recover_deliveries(core.store, core.clock())
                    raise httpx.RemoteProtocolError("synthetic unknown Core response",
                                                    request=request)

            async def lose_settlement(response):
                if response.request.url.path.endswith("/expired-preflight"):
                    assert response.status_code == 200
                    proofs.append(json.loads(response.request.content))
                    if len(proofs) == 1:
                        raise httpx.ReadError("synthetic lost cancellation ACK",
                                              request=response.request)

            active.client.event_hooks["request"].append(lose_begin)
            active.client.event_hooks["response"].append(lose_settlement)
            with pytest.raises(httpx.ReadError):
                await active.pump()
            assert active.attempts() == {"ACK": 1, "RESERVED": 1}
            active.restart()
            assert active.runtime.row()["state"] == "ACTIVE"
            active.client.event_hooks["request"].remove(lose_begin)
            assert await active.pump()
            assert proofs == [proofs[0], proofs[0]]
            assert active.attempts() == {"ACK": 1, "NOT_SENT": 1} and len(sends) == 1
            active.client.event_hooks["response"].remove(lose_settlement)
            forged = dict(proofs[0], reserved_record_sha256="0" * 64)
            denied = await active.client.post("/internal/v1/owner-authority/expired-preflight",
                                              json=forged)
            assert denied.status_code == 403
            assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid") == usage
            raw = encode(raw_event(2, text="合成的新消息", time=int(core.clock.now.timestamp())))
            event = normalize(raw, active.config, now=core.clock.now.timestamp())
            assert active.runtime.persist_capture(event, raw, received_at=core.clock.now,
                                                  peer=("127.0.0.1", 3001))
            await active.flush()
            cfg = tmp_path / "next-owner.json"
            cfg.write_text(json.dumps(core.settings.model_dump(mode="python"),
                default=lambda value: value.get_secret_value()
                if isinstance(value, SecretStr) else str(value)), encoding="utf8")
            assert await child("tests.owner_runtime_process", "--config", str(cfg),
                "--now", core.clock(), "--reply", "合成的新回复") == {"worked": True, "calls": 1}
            assert await active.pump() and len(sends) == 2
            assert active.attempts() == {"ACK": 2, "NOT_SENT": 1}
            assert active.runtime.row()["state"] == "ACTIVE"
            assert core.store.rows("SELECT * FROM model_usage ORDER BY rowid")[:1] == usage

    run(scenario())


def test_proactive_provider_response_loss_still_stops_without_resending(core, tmp_path):
    provider_calls = 0

    def disconnect(request):
        nonlocal provider_calls
        if request.url.path == "/send_private_msg":
            provider_calls += 1
            if provider_calls == 2:
                raise httpx.ReadError("synthetic provider response lost", request=request)

    async def scenario():
        async with ready(core, tmp_path, protocol_reply=disconnect) as (active, sends):
            assert await active.pump()
            assert await active.pump()
            assert provider_calls == 2 and len(sends) == 1
            assert active.attempts() == {"ACK": 1, "UNKNOWN": 1}
            assert active.runtime.row()["state"] == "STOPPED"
            assert core.store.rows("SELECT state FROM outbox ORDER BY segment_index") == [
                {"state": "SENT"}, {"state": "DELIVERY_UNCERTAIN"}, {"state": "CANCELLED"}]
            active.restart()
            with pytest.raises(ValueError, match="^OWNER_RUNTIME_STOPPED$"):
                await active.pump()
            assert provider_calls == 2

    run(scenario())
