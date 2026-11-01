"""An explicit voice-only revision preserves native carry and original liabilities."""

import hashlib
import json
import ssl
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from companion import model_carry, owner_authority, owner_input, voice_authority
from companion.contracts import CoreError, InboundEvent
from companion.owner_authority_protocol import AuthorityStop, OwnerCapture, OwnerGrantPolicy
from companion.qq_rich import input_digest
from companion.voice_config import VoiceNode
from companion.voice_protocol import digest
from companion_delivery_protocol.authority import capture_signing_payload
from companion_voice_node.worker import local_manifest
from tests import owner_model_fixtures
from tests.conftest import event, run
from tests.model_carry_support import migrated
from tests.test_model_carry_lifecycle import current_unknown, stop_current
from tests.test_model_renewal import renewal
from tests.test_owner_authority import activate
from tests.test_voice_jobs import voice_config
from tests.test_voice_node_api import independent_node, tls_core
from tests.test_voice_node_worker import node as node  # noqa: F401 -- pytest fixture
from tests.voice_owner_fixtures import native_reply


def carried_voice(core, tmp_path, monkeypatch):
    core.settings.voice = voice_config()

    def policy(**values):
        return OwnerGrantPolicy(**{**values, "purposes": ["chat", "voice"],
            "capability_policies": {"voice": core.settings.voice.authority_sha256()}})

    with monkeypatch.context() as patch:
        patch.setattr(owner_model_fixtures, "OwnerGrantPolicy", policy)
        return migrated(core, tmp_path)


def revised(core, case):
    core.settings.voice = core.settings.voice.model_copy(update={
        "reply": core.settings.voice.reply.model_copy(update={"max_chars": 199})})
    return case.policy.model_copy(update={"authorization_sha256": "a" * 64,
        "capability_policies": {**case.policy.capability_policies,
            "voice": core.settings.voice.authority_sha256()}})


def test_voice_revision_and_later_renewal_retain_original_liability(core, tmp_path, monkeypatch):
    case = carried_voice(core, tmp_path, monkeypatch)
    before = core.store.rows("SELECT * FROM model_usage")
    stop_current(core, case)
    policy = revised(core, case)
    active, _ = activate(core, policy, revision=4, operation="approve-exact-voice-revision")
    assert voice_authority.current(core.store, core.settings, core.clock).authority.as_dict() == (
        active["authority"])
    assert core.store.rows("SELECT * FROM model_usage") == before
    case.policy, case.successor = policy, active
    request, renewed, witness = renewal(core, case)
    receipt = model_carry.prepare(core.store, core.settings, request, witness, core.clock())
    assert receipt["retained_micro_usd"] == before[0]["billed_micro_usd"]
    activate(core, renewed, revision=6, operation="renew-after-exact-voice-revision")
    current = voice_authority.current(core.store, core.settings, core.clock)
    assert current.policy.capability_policies == policy.capability_policies
    assert core.store.rows("SELECT * FROM model_usage") == before
    assert not core.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("change", ["authorization", "wrong_voice", "disabled", "purposes",
    "owner", "capture", "retention", "carry", "paid_policy", "paid_config", "other_capability",
    "expiry", "reply_limit"])
def test_voice_revision_cannot_expand_or_rebind_other_scope(core, tmp_path, monkeypatch, change):
    case = carried_voice(core, tmp_path, monkeypatch)
    stop_current(core, case)
    policy = revised(core, case)
    changes = {
        "authorization": {"authorization_sha256": case.policy.authorization_sha256},
        "wrong_voice": {"capability_policies": {"voice": "0" * 64}},
        "purposes": {"purposes": [*policy.purposes, "evaluation"]},
        "owner": {"owner_qq_id": "99999"},
        "capture": {"capture": policy.capture.model_copy(update={"protocol_epoch": "other-epoch"})},
        "retention": {"source_retention_days": 30},
        "carry": {"unknown_carry_sha256": "b" * 64},
        "paid_policy": {"paid_contract_sha256": "b" * 64},
        "other_capability": {"purposes": [*policy.purposes, "image"],
            "capability_policies": {**policy.capability_policies, "image": "b" * 64}},
        "expiry": {"expires_at": datetime(2099, 1, 1, tzinfo=UTC)},
        "reply_limit": {"max_reply_chars": 123},
    }
    policy = policy.model_copy(update=changes.get(change, {}))
    if change == "disabled":
        core.settings.voice = core.settings.voice.model_copy(update={"enabled": False})
    if change == "paid_config":
        core.settings.paid_model = core.settings.paid_model.model_copy(update={"window_calls": 999})
    journal = Path(core.settings.paid_model.journal_path)
    before, finance = journal.read_bytes(), core.store.rows("SELECT * FROM model_usage")
    with pytest.raises(CoreError):
        activate(core, policy, revision=4, operation="reject-voice-scope-drift")
    assert journal.read_bytes() == before
    assert core.store.rows("SELECT * FROM model_usage") == finance
    assert owner_authority._latest(core.store)["state"] == "STOPPED"


def test_current_unknown_still_requires_same_policy_handoff(core, tmp_path, monkeypatch):
    case = carried_voice(core, tmp_path, monkeypatch)
    request_id = current_unknown(core)
    stop_current(core, case)
    before = core.store.rows("SELECT * FROM model_usage")
    original_voice, policy = core.settings.voice, revised(core, case)
    with pytest.raises(CoreError, match="OWNER_MODEL_CARRY_NOT_QUIESCENT"):
        activate(core, policy, revision=4, operation="reject-voice-current-unknown")
    core.settings.voice = original_voice
    same, _ = activate(core, case.policy, revision=4, operation="preserve-voice-unknown-handoff")
    case.successor = same
    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
        operation_id="voice-after-normal-handoff-stop", authority=same["authority"]))
    revised(core, case)
    active, _ = activate(core, policy, revision=6, operation="voice-after-original-handoff")
    assert active["authority"]["revision"] == 7
    assert core.store.rows("SELECT * FROM model_usage") == before
    assert core.store.one("SELECT state FROM model_usage WHERE request_id=?",
                          (request_id,))["state"] == "UNKNOWN"


def capture_text(core, key, authority, policy):
    source = (f"{core.settings.bot_qq_id}:onebot-direct:FriendMessage:"
              f"{core.settings.owner_qq_id}:epoch:{policy.capture.protocol_epoch}:303")
    incoming = event(core, text="synthetic voice revision request",
        origin="onebot-direct:FriendMessage:" + core.settings.owner_qq_id).model_copy(update={
        "source_event_id": source,
        "event_id": "evt_" + hashlib.sha256(source.encode()).hexdigest()})
    value = dict(authority=authority, kind="authenticated_onebot_ws", source_event_id=source,
        event_id=incoming.event_id, input_sha256=input_digest({"text": incoming.payload.text}),
        frame_sha256="b" * 64, evidence_sha256="c" * 64,
        identity_sha256=policy.capture.identity_sha256,
        protocol_epoch=policy.capture.protocol_epoch,
        occurred_at=core.clock(), received_at=core.clock())
    value["signature"] = key.sign(capture_signing_payload(value)).hex()
    owner_input.approve(core.store, core.settings, core.clock, OwnerCapture.model_validate(value))
    core.ingest(InboundEvent.model_validate(incoming), "voice-revision-real-source")


def test_http_voice_revision_runs_independent_voice_node(core, tmp_path, monkeypatch, node):
    case = carried_voice(core, tmp_path, monkeypatch)
    stop_current(core, case)
    config, signing_key, counter = node
    config = config.model_copy(update={"node_id": "local", "heartbeat_seconds": 0.5,
        "execution_timeout_seconds": 20.0, "request_timeout_seconds": 10.0})
    core.settings.voice = voice_config(signing_private_key=signing_key.private_bytes_raw().hex(),
        nodes={"local": VoiceNode(token=config.credential,
                                  manifest_sha256=digest(local_manifest(config)))})
    policy = case.policy.model_copy(update={"authorization_sha256": "a" * 64,
        "capability_policies": {"voice": core.settings.voice.authority_sha256()}})
    core.settings.control_token = SecretStr("synthetic-voice-revision-control")
    original = core.store.rows("SELECT * FROM model_usage")

    async def scenario():
        async with tls_core(core, tmp_path) as (origin, certificate), httpx.AsyncClient(
            base_url=origin, verify=ssl.create_default_context(cafile=str(certificate)),
            trust_env=False,
        ) as http:
            query = {"expected_revision": 4, "policy": policy.model_dump(mode="json")}
            denied = await http.post("/internal/v1/owner-authority/preview", json=query)
            assert denied.status_code == 401
            headers = {"Authorization": "Bearer synthetic-voice-revision-control"}
            preview = await http.post("/internal/v1/owner-authority/preview", json=query,
                                      headers=headers)
            assert preview.status_code == 200, preview.text
            activation = {**query, "operation_id": "native-voice-capability-revision",
                          "preview_sha256": preview.json()["preview_sha256"]}
            active = await http.post("/internal/v1/owner-authority/activate", headers=headers,
                                     json=activation)
            assert active.status_code == 200, active.text
            repeated = await http.post("/internal/v1/owner-authority/activate", headers=headers,
                                       json=activation)
            assert repeated.status_code == 200 and repeated.json() == active.json()
            assert core.store.rows("SELECT * FROM model_usage") == original

    # The subsequent independent worker opens the same persisted Core after the
    # authenticated HTTP revision; its provider transport is synthetic.
    run(scenario())
    authority = owner_authority.current(core.store, core.settings, core.clock).authority.as_dict()
    capture_text(core, case.key, authority, policy)
    message = native_reply(core, "synthetic voice revision reply")
    with core.store.transaction():
        assert core.voice.prepare_reply(message).state == "WAITING"

    async def worker():
        async with tls_core(core, tmp_path) as (origin, certificate):
            await independent_node(config.model_copy(update={"core_origin": origin}), certificate,
                counter, tmp_path, core.clock.now.timestamp())

    run(worker())
    row = core.store.one("SELECT * FROM voice_jobs WHERE message_id=?", (message,))
    assert row["state"] == "READY" and json.loads(row["authority_json"]) == authority
    assert core.store.rows("SELECT * FROM model_usage WHERE state='UNKNOWN'") == [original[0]]
    assert core.store.one("SELECT state FROM outbox WHERE message_id=?", (message,))["state"] == (
        "PENDING")
