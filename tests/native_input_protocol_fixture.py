"""Original signed input and recall over a separately opened native HTTP Core."""

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
from pydantic import SecretStr

from companion.core import Core
from companion.model import ModelClient
from companion.retrieval import SemanticIndex
from companion.store import encode
from companion_delivery_protocol.authority import capture_signing_payload, recall_signing_payload
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.event_normalizer import normalize, normalize_recall
from companion_onebot_bridge.rich_input import input_digest
from tests.support.core_http import real_core_http
from tests.support.onebot_events import raw_event
from tests.test_owner_input import captured

BASE = "/internal/v1/owner-authority"


@asynccontextmanager
async def environment(core, tmp_path, monkeypatch):
    core.clock.now = datetime.now(UTC).replace(microsecond=0)
    core.settings.control_token = SecretStr("synthetic-native-input-control-token")
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    original, key = captured(core, tmp_path)
    config = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
        bot_qq_id=core.settings.bot_qq_id,
        bridge_token=core.settings.bridge_token.get_secret_value(),
        onebot_token="s" * 32, protocol_epoch=original.protocol_epoch,
        capture_signing_key=key.private_bytes_raw().hex())
    grant = SimpleNamespace(authority=original.authority, key=key, captures={},
                             identity_sha256=original.identity_sha256)
    with Core(core.settings, clock=core.clock) as api_core:
        assert api_core.store is not core.store
        async with real_core_http(api_core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + config.bridge_token},
        ) as client:
            yield client, config, grant, core.clock.now


async def admit(client, config, grant, now, number=1):
    raw = raw_event(number, time=int(now.timestamp()), text="合成主人输入" + str(number))
    event = normalize(encode(raw), config, now=now.timestamp())
    capture = {"kind": "authenticated_onebot_ws", "authority": grant.authority,
        "event_id": event["event_id"], "source_event_id": event["source_event_id"],
        "input_sha256": input_digest(event["payload"]),
        "frame_sha256": hashlib.sha256(encode(raw).encode()).hexdigest(),
        "evidence_sha256": "e" * 64, "identity_sha256": grant.identity_sha256,
        "protocol_epoch": config.protocol_epoch,
        "received_at": now.isoformat(), "occurred_at": event["occurred_at"]}
    capture["signature"] = grant.key.sign(capture_signing_payload(capture)).hex()
    grant.captures[event["event_id"]] = capture
    approved = await client.post(BASE + "/inputs/approve", json=capture)
    assert approved.status_code == 200, approved.text
    accepted = await client.post("/internal/v1/inbox/events", json=event,
                                  headers={"X-Idempotency-Key": event["event_id"]})
    return event, accepted


def notice(config, grant, now, number=1):
    frame = encode({"time": int(now.timestamp()), "self_id": int(config.bot_qq_id),
        "post_type": "notice", "notice_type": "friend_recall",
        "user_id": int(config.owner_qq_id), "message_id": number})
    value = {"kind": "authenticated_onebot_friend_recall", "authority": grant.authority,
        **normalize_recall(frame, config, now=now.timestamp()),
        "protocol_epoch": config.protocol_epoch, "identity_sha256": grant.identity_sha256,
        "received_at": now.isoformat(), "frame_sha256": hashlib.sha256(frame.encode()).hexdigest(),
        "evidence_sha256": "e" * 64}
    value["signature"] = grant.key.sign(recall_signing_payload(value)).hex()
    return value


async def state(client, grant, event):
    return await client.post(BASE + "/inputs/state", json=grant.captures[event["event_id"]])


async def reply(core, client, grant):
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "合成回复"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        with Core(core.settings, ModelClient(core.settings, http), core.clock) as worker:
            assert worker.store is not core.store
            assert await worker.run_once("native-input-protocol")
    assert len(calls) == 1
    leased = await client.post("/internal/v1/deliveries/lease", json={
        "worker_id": "native-input-bridge", "authority": grant.authority})
    assert leased.status_code == 200, leased.text
    return leased.json()["items"][0]
