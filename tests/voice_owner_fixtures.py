"""Signed OwnerAuthority voice input; no window or replacement permission guards."""

import hashlib
import json

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import owner_input, paid_budget
from companion.contracts import InboundEvent
from companion.owner_authority_protocol import OwnerCapture, OwnerGrantPolicy, OwnerRuntimeConfig
from companion.qq_rich import input_digest
from companion.store import after
from companion_delivery_protocol.authority import capture_signing_payload
from tests.conftest import event
from tests.support.owner_authority import activate
from tests.support.paid_model import approved_live
from tests.support.voice_jobs import voice_config


def native_voice(core, tmp_path, *, record=True, record_count=1,
                 expires_seconds=None, voice=True, config=None):
    approved_live(core, tmp_path)
    core.settings.rich_input_scope = "references_v1"
    core.settings.voice = config or voice_config(reply={"mode": "auto"})
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    key = Ed25519PrivateKey.generate()
    identity = paid_budget.sha([core.settings.owner_qq_id, core.settings.bot_qq_id])
    policy = OwnerGrantPolicy(
        owner_qq_id=core.settings.owner_qq_id, purposes=["chat", "voice"] if voice else ["chat"],
        authorization_sha256="d" * 64,
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capability_policies={"voice": core.settings.voice.authority_sha256()} if voice else {},
        capture={"public_key": key.public_key().public_bytes_raw().hex(),
                 "identity_sha256": identity, "protocol_epoch": "voice-owner-native"},
        expires_at=after(core.clock(), expires_seconds) if expires_seconds is not None else None,
    )
    result, _ = activate(core, policy)
    return capture_voice_event(core, key, result["authority"], record=record,
                               record_count=record_count)


def capture_voice_event(core, key, authority, *, record=True, record_count=1, number=123):
    identity = paid_budget.sha([core.settings.owner_qq_id, core.settings.bot_qq_id])
    source = (f"{core.settings.bot_qq_id}:onebot-direct:FriendMessage:"
              f"{core.settings.owner_qq_id}:epoch:voice-owner-native:{number}")
    message = event(core, text="" if record else "synthetic voice reply request",
                    origin="onebot-direct:FriendMessage:" + core.settings.owner_qq_id).model_dump(
                        mode="json")
    message.update(event_id="evt_" + hashlib.sha256(source.encode()).hexdigest(),
                   source_event_id=source)
    if record:
        message["payload"]["rich"] = {
            "version": "references_v1", "coverage": "PARTIAL", "segment_count": record_count,
            "owner_text_positions": [], "segments": [{"index": index, "type": "record",
                "actor": "UNVERIFIED", "coverage": "UNSUPPORTED"} for index in range(record_count)],
        }
    incoming = InboundEvent.model_validate(message)
    value = dict(authority=authority, kind="authenticated_onebot_ws",
                 source_event_id=source, event_id=message["event_id"],
                 input_sha256=input_digest(message["payload"]), frame_sha256="b" * 64,
                 evidence_sha256="c" * 64, identity_sha256=identity,
                 protocol_epoch="voice-owner-native", occurred_at=core.clock(),
                 received_at=core.clock())
    value["signature"] = key.sign(capture_signing_payload(value)).hex()
    capture = OwnerCapture.model_validate(value)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    accepted = core.ingest(incoming, "native-voice-source-" + str(number))
    return capture, key, accepted["job_id"]


def native_reply(core, text):
    """Actual SDK admission/settlement and original Outbox custody; provider is synthetic."""
    from companion.model import ModelClient
    from tests.conftest import run

    event = core.store.one(
        "SELECT * FROM events WHERE source='qq.onebot' ORDER BY "
        'record_sequence DESC'
    )
    status = core.voice.input_status(event["event_id"])
    if status.state == "WAITING":
        core.clock.advance(core.settings.voice.asr.wait_seconds + 1)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={"choices": [{"message": {"content": text}}],
                       "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
        ))) as client:
            core.model = ModelClient(core.settings, client)
            assert await core.run_once("native-voice-test-reply")
    previous_model = core.model
    try:
        run(scenario())
    finally:
        core.model = previous_model
    outbox = core.store.one("SELECT * FROM outbox ORDER BY record_sequence DESC LIMIT 1")
    assert outbox is not None and json.loads(outbox["content_json"])["text"] == text
    return outbox["message_id"]
