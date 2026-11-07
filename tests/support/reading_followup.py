import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import paid_budget
from companion.config import ObservationAdapterConfig
from companion.core import Core
from companion.owner_authority_protocol import OwnerGrantPolicy
from companion.retrieval import SemanticIndex
from companion.store import after, encode
from companion_delivery_protocol.authority import capture_signing_payload
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.rich_input import input_digest
from tests.support.core_http import real_core_http
from tests.support.exploration import URL
from tests.support.onebot_events import raw_event
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare
from tests.support.reading_protocol import envelope
from tests.support.reading_service import TOKEN

SHORT = "https://b23.tv/abc123"


READER_AUTH = {"Authorization": "Bearer " + TOKEN}


@asynccontextmanager
async def window(core, tmp_path, monkeypatch, *, context_soft_tokens=None):
    now = datetime.now(UTC).replace(microsecond=0)
    core.clock.now = now
    # This Reader fixture has no memory rows; it does not need an embedding model download.
    monkeypatch.setattr(SemanticIndex, "warmup", lambda self: None)
    policy = prepare(core, tmp_path)
    if context_soft_tokens is not None:
        core.settings.context_soft_tokens = context_soft_tokens
    core.settings.content_enabled = True
    core.settings.rich_input_scope = "references_v1"
    core.settings.observation_enabled = True
    core.settings.exploration_enabled = True
    core.settings.observation_adapters = {"bili": ObservationAdapterConfig(
        source="bilibili", token=TOKEN, reader_max_requests=6)}
    core.settings.exploration_daily_requests = 12
    core.settings.exploration_monthly_requests = 24
    core.settings.exploration_daily_bytes = 8388608
    key = Ed25519PrivateKey.generate()
    config = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
        bot_qq_id=core.settings.bot_qq_id,
        bridge_token=core.settings.bridge_token.get_secret_value(),
        onebot_token="s" * 32, protocol_epoch="sdk-reader-epoch")
    policy = OwnerGrantPolicy.model_validate({**policy.model_dump(),
        "purposes": ["chat", "content", "history", "exploration"],
        "capture": {"public_key": key.public_key().public_bytes_raw().hex(),
            "identity_sha256": paid_budget.sha([config.owner_qq_id, config.bot_qq_id]),
            "protocol_epoch": config.protocol_epoch},
        "history": {"scope": "owner_private", "not_before": after(core.clock(), -3600),
            "max_turns": 16, "max_utf8_bytes": 64000}})
    activation, _ = activate(core, policy)
    plan = SimpleNamespace(authority=activation["authority"], key=key, policy=policy)
    with Core(core.settings, clock=core.clock) as http_core:
        assert http_core.store is not core.store
        async with real_core_http(http_core) as port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"Authorization": "Bearer " + config.bridge_token},
        ) as client:
            yield client, config, plan


async def admit(client, core, config, plan, number, text="", *, card=None, reference=None):
    now = core.clock.now
    raw = raw_event(number, time=int(now.timestamp()), text=text)
    if card or reference is not None:
        raw["message"] = [{"type": "text", "data": {"text": text}}] if text else []
        if card:
            raw["message"].append({"type": "json", "data": {"data": encode({
                "meta": {"news": {"title": "恒星资料", "jumpUrl": card}},
            })}})
        if reference is not None:
            raw["message"].append({"type": "reply", "data": {"id": str(reference)}})
    event = normalize(encode(raw), config, now=now.timestamp(), rich_input_scope="references_v1")
    capture = {
        "kind": "authenticated_onebot_ws", "authority": plan.authority,
        "event_id": event["event_id"], "source_event_id": event["source_event_id"],
        "input_sha256": input_digest(event["payload"]),
        "frame_sha256": hashlib.sha256(encode(raw).encode()).hexdigest(),
        "evidence_sha256": "e" * 64, "identity_sha256": plan.policy.capture.identity_sha256,
        "protocol_epoch": config.protocol_epoch,
        "received_at": now.isoformat(), "occurred_at": event["occurred_at"],
    }
    capture["signature"] = plan.key.sign(capture_signing_payload(capture)).hex()
    approved = await client.post("/internal/v1/owner-authority/inputs/approve", json=capture)
    assert approved.status_code == 200, approved.text
    accepted = await client.post("/internal/v1/inbox/events", json=event,
                                headers={"X-Idempotency-Key": event["event_id"]})
    assert accepted.status_code == 202, accepted.text
    return event, accepted.json()


def notice(config, plan, now, *, number):
    from companion.input_recall import canonical_source
    from companion_delivery_protocol.authority import recall_signing_payload

    source = canonical_source(bot_qq_id=config.bot_qq_id, owner_qq_id=config.owner_qq_id,
        protocol_epoch=config.protocol_epoch, provider_message_id=number)["source_event_id"]
    stamp = now.astimezone(UTC).isoformat()
    value = dict(authority=plan.authority, kind="authenticated_onebot_friend_recall",
        recall_id="recall_" + hashlib.sha256((source + stamp).encode()).hexdigest(),
        target_source_event_id=source, target_event_id="evt_" + hashlib.sha256(
            source.encode()).hexdigest(), protocol_epoch=config.protocol_epoch,
        identity_sha256=plan.policy.capture.identity_sha256, occurred_at=stamp,
        received_at=stamp, frame_sha256="f" * 64, evidence_sha256="a" * 64)
    value["signature"] = plan.key.sign(recall_signing_payload(value)).hex()
    return value


async def lease(client):
    leased = await client.post("/internal/v3/readers/bili/lease", json={}, headers=READER_AUTH)
    assert leased.status_code == 200, leased.text
    return leased.json()["item"]


async def validate(client, item):
    return await client.post("/internal/v3/readers/bili/" + item["request_id"] + "/validate",
                             json={"lease_token": item["lease_token"]}, headers=READER_AUTH)


async def finish(client, core, item, *, status="SUCCESS"):
    body = envelope("bilibili")
    body["lease_token"] = item["lease_token"]
    row = body["observations"][0]
    row["provenance"] = item["context"]
    row["fetched_at"] = row["evidence"]["fetched_at"] = core.clock()
    if status != "SUCCESS":
        body.update(status=status, observations=[], error_code="UPSTREAM_TIMEOUT")
        if status == "UNKNOWN":
            body.update(used_requests=None, used_bytes=None)
    result = await client.post("/internal/v3/readers/bili/" + item["request_id"] + "/result",
                               json=body, headers=READER_AUTH)
    assert result.status_code == 200, result.text
    return result.json(), body


async def select_read(core, url=URL):
    """A synthetic model selects the SDK tool; ingestion itself does not read."""
    from tests.sdk_reader_fixture import Provider, answer, call, worker_for

    provider = Provider(answer(calls=(call("select_read", "read_content", {"url": url}),)),
                        answer("按读取回执说明当前状态。"))
    async with worker_for(core, provider) as worker:
        assert await worker.run_once("select-original-read")
    return provider
