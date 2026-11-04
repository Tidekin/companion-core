"""Signed native input, original grants and paid receipts for private-poke checks."""

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import budget, owner_authority, owner_input, paid_budget, poke
from companion.contracts import InboundEvent
from companion.model import ModelClient
from companion.owner_authority_protocol import OwnerCapture, OwnerGrantPolicy, OwnerRuntimeConfig
from companion.poke_protocol import OwnerPokeCapture, PokePolicy, canonical_source, signed_payload
from companion.store import after, encode
from companion.transport import enqueue
from companion_delivery_protocol.authority import capture_signing_payload
from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.rich_input import input_digest
from tests.conftest import run
from tests.support.onebot_events import raw_event
from tests.test_owner_authority import activate
from tests.test_paid_model_gate import approved_live


def environment(core, tmp_path):
    approved_live(core, tmp_path)
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    core.settings.poke_policy = PokePolicy(enabled=True, allow_native=True)
    key = Ed25519PrivateKey.generate()
    config = BridgeConfig(owner_qq_id=core.settings.owner_qq_id,
        bot_qq_id=core.settings.bot_qq_id,
        bridge_token=core.settings.bridge_token.get_secret_value(),
        onebot_token="s" * 32, protocol_epoch="native-poke-epoch")
    policy = OwnerGrantPolicy(owner_qq_id=config.owner_qq_id,
        purposes=["chat", "poke", "history"], authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capability_policies={"poke": paid_budget.sha(core.settings.poke_policy.model_dump())},
        history={"scope": "owner_private", "not_before": after(core.clock(), -86400),
                 "max_turns": 15, "max_utf8_bytes": 16000},
        capture={"public_key": key.public_key().public_bytes_raw().hex(),
            "identity_sha256": paid_budget.sha([config.owner_qq_id, config.bot_qq_id]),
            "protocol_epoch": config.protocol_epoch})
    activate(core, policy)
    return SimpleNamespace(core=core, store=core.store, clock=core.clock, settings=core.settings,
        key=key, config=config,
        approval=owner_authority.current(core.store, core.settings, core.clock))


def refresh(env):
    policy = OwnerGrantPolicy.model_validate({**env.approval.policy.model_dump(),
        "capability_policies": {"poke": paid_budget.sha(env.settings.poke_policy.model_dump())}})
    activate(env.core, policy, revision=env.approval.authority.revision,
             operation="poke-policy-" + str(env.approval.authority.revision + 1))
    env.approval = owner_authority.current(env.store, env.settings, env.clock)


def signed(env, *, timestamp=None, **changes):
    now = datetime.fromisoformat(env.clock())
    stamp = int(now.timestamp()) if timestamp is None else timestamp
    notice = {"time": stamp, "self_id": env.config.bot_qq_id, "user_id": env.config.owner_qq_id,
        "sender_id": env.config.owner_qq_id, "target_id": env.config.bot_qq_id,
        "action": "拍了拍", "suffix": ""}
    source = canonical_source(notice)
    value = {"kind": "authenticated_onebot_poke", "authority": env.approval.authority.as_dict(),
        "event_id": source["event_id"], "source_event_id": source["source_event_id"],
        "notice": notice, "protocol_epoch": env.config.protocol_epoch,
        "identity_sha256": env.approval.policy.capture.identity_sha256,
        "config_sha256": "c" * 64, "frame_sha256": "f" * 64, "evidence_sha256": "e" * 64,
        "occurred_at": datetime.fromtimestamp(stamp, UTC).isoformat(),
        "received_at": now.isoformat(), **changes}
    value["signature"] = env.key.sign(signed_payload(value)).hex()
    return OwnerPokeCapture.model_validate(value)


def owner_turn(env, number, text, *, delivered=True, approve_only=False):
    now = datetime.fromisoformat(env.clock())
    raw = raw_event(number, time=int(now.timestamp()), text=text)
    event = normalize(json.dumps(raw), env.config, now=now.timestamp())
    value = {"kind": "authenticated_onebot_ws", "authority": env.approval.authority.as_dict(),
        "event_id": event["event_id"], "source_event_id": event["source_event_id"],
        "input_sha256": input_digest(event["payload"]),
        "frame_sha256": hashlib.sha256(encode(raw).encode()).hexdigest(),
        "evidence_sha256": "e" * 64,
        "identity_sha256": env.approval.policy.capture.identity_sha256,
        "protocol_epoch": env.config.protocol_epoch,
        "received_at": now.isoformat(), "occurred_at": event["occurred_at"]}
    value["signature"] = env.key.sign(capture_signing_payload(value)).hex()
    owner_input.approve(env.store, env.settings, env.clock, OwnerCapture.model_validate(value))
    if approve_only:
        return event
    result = env.core.ingest(InboundEvent.model_validate(event), event["event_id"])
    if delivered:
        with env.store.transaction():
            source = env.store.one("SELECT * FROM events WHERE event_id=?", (event["event_id"],))
            message = enqueue(env.store, event["event_id"], source["conversation_id"],
                              "收到：" + text, env.clock())
            env.store.execute("UPDATE outbox SET state='SENT',sent_at=? WHERE message_id=?",
                              (env.clock(), message))
            env.store.execute("UPDATE jobs SET state='SUCCEEDED' WHERE job_id=?",
                              (result["job_id"],))
    return result


def settle(env, job, decision):
    """Run the actual paid admission once before testing domain commit or delivery."""
    messages = poke.context(env.store, env.settings, job, clock=env.clock)
    request_id = budget.reserve(env.store, env.settings, job["job_id"], "qq_poke",
                                messages, env.clock(), clock=env.clock)

    async def complete():
        def provider(request):
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps(decision)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            await budget.complete(ModelClient(env.settings, client), env.store, env.settings,
                request_id, messages, "qq_poke", env.clock,
                source_check=lambda: poke.check_job(env.store, env.settings, env.approval,
                                                    job, clock=env.clock))

    run(complete())
