"""Native signed owner ingress for discovery/caption tests; no window authority."""

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from companion import owner_authority, paid_budget
from companion.core import Core
from companion.owner_authority_protocol import OwnerGrantPolicy, OwnerRuntimeConfig
from companion.retrieval import SemanticIndex
from companion.store import encode
from companion_delivery_protocol.authority import capture_signing_payload
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.rich_input import input_digest
from tests.support.core_http import real_core_http
from tests.support.onebot_events import raw_event
from tests.support.owner_authority import activate
from tests.support.paid_model import approved_live
from tests.test_reading_followup import notice as reader_notice


@asynccontextmanager
async def environment(core, tmp_path, monkeypatch, *, paid=None, purposes=(), history=None):
    core.clock.now = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    approved_live(core, tmp_path, **(paid or {}))
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    core.settings.expression_enabled = True
    core.settings.control_token = SecretStr("synthetic-meme-owner-control-token")
    key = Ed25519PrivateKey.generate()
    config = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
        bot_qq_id=core.settings.bot_qq_id,
        bridge_token=core.settings.bridge_token.get_secret_value(),
        onebot_token="s" * 32, protocol_epoch="native-meme-epoch")
    capability = owner_authority.capability_hashes(core.settings)["meme"]
    policy = OwnerGrantPolicy(owner_qq_id=config.owner_qq_id,
        purposes=["chat", "expression", *(["meme"] if capability else []), *purposes],
        memory_scope="owner_private" if "memory" in purposes else "none",
        **({"history": history} if history else {}),
        capability_policies={"meme": capability} if capability else {},
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capture={"public_key": key.public_key().public_bytes_raw().hex(),
            "identity_sha256": paid_budget.sha([config.owner_qq_id, config.bot_qq_id]),
            "protocol_epoch": config.protocol_epoch})
    active, _ = activate(core, policy)
    grant = SimpleNamespace(authority=active["authority"], key=key, policy=policy,
                            control_token=core.settings.control_token.get_secret_value())
    with Core(core.settings, clock=core.clock) as api_core:
        async with real_core_http(api_core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + config.bridge_token},
        ) as client:
            yield client, config, grant, core.clock.now


async def admit(client, config, grant, now, number=1, *, text=None):
    raw = raw_event(number, time=int(now.timestamp()),
                    text=text if text is not None else "合成主人输入" + str(number))
    event = normalize(json.dumps(raw), config, now=now.timestamp())
    value = {"kind": "authenticated_onebot_ws", "authority": grant.authority,
        "event_id": event["event_id"], "source_event_id": event["source_event_id"],
        "input_sha256": input_digest(event["payload"]),
        "frame_sha256": hashlib.sha256(encode(raw).encode()).hexdigest(),
        "evidence_sha256": "e" * 64,
        "identity_sha256": grant.policy.capture.identity_sha256,
        "protocol_epoch": config.protocol_epoch,
        "received_at": now.isoformat(), "occurred_at": event["occurred_at"]}
    value["signature"] = grant.key.sign(capture_signing_payload(value)).hex()
    approved = await client.post("/internal/v1/owner-authority/inputs/approve", json=value)
    assert approved.status_code == 200, approved.text
    response = await client.post("/internal/v1/inbox/events", json=event,
                                headers={"X-Idempotency-Key": event["event_id"]})
    return event, response


def notice(config, grant, now, number=1):
    return reader_notice(config, grant, now, number=number)


async def stop(client, grant):
    return await client.post("/internal/v1/owner-authority/stop", json={
        "operation_id": "stop-meme-native", "authority": grant.authority},
        headers={"Authorization": "Bearer " + grant.control_token})
