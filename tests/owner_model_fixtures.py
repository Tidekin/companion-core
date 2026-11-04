"""Signed native chat custody for isolated model/admission boundary checks."""

import hashlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import owner_authority, owner_input, paid_budget
from companion.owner_authority_protocol import OwnerCapture, OwnerGrantPolicy, OwnerRuntimeConfig
from companion.qq_rich import input_digest
from companion_delivery_protocol.authority import capture_signing_payload
from tests.conftest import event


def authorized_job(core, *, lease_seconds=None, purposes=("chat",), number=1, renew=False):
    """Keep the test's paid policy and return one genuinely admitted, leased job."""
    from tests.support.owner_authority import activate

    if core.settings.mode != "live":
        return None
    jobs = {} if renew else getattr(core, "authorized_model_test_jobs", {})
    if number in jobs:
        return jobs[number]
    core.settings.owner_runtime = OwnerRuntimeConfig(mode="ENABLED")
    key = Ed25519PrivateKey.generate()
    identity = paid_budget.sha([core.settings.owner_qq_id, core.settings.bot_qq_id])
    policy = OwnerGrantPolicy(
        owner_qq_id=core.settings.owner_qq_id, purposes=list(purposes),
        authorization_sha256="d" * 64,
        memory_scope="owner_private" if "memory" in purposes else "none",
        capability_policies={name: value for name, value in
                             owner_authority.capability_hashes(core.settings).items()
                             if name in purposes},
        paid_contract_sha256=paid_budget.sha(paid_budget.snapshot(core.settings)),
        capture={"public_key": key.public_key().public_bytes_raw().hex(),
                 "identity_sha256": identity, "protocol_epoch": "model-boundary-test"},
    )
    if renew:
        previous, _ = core.authorized_model_test_capture
        revision = previous["authority"]["revision"]
        active, _ = activate(core, policy, revision=revision,
                             operation=f"model-boundary-renew-{revision}")
        core.authorized_model_test_capture = active, key
    elif hasattr(core, "authorized_model_test_capture"):
        active, key = core.authorized_model_test_capture
    else:
        active, _ = activate(core, policy)
        core.authorized_model_test_capture = active, key
    counter = getattr(core, "authorized_model_source_counter", 0) + 1
    core.authorized_model_source_counter = counter
    source = (f"{core.settings.bot_qq_id}:onebot-direct:FriendMessage:"
              f"{core.settings.owner_qq_id}:epoch:model-boundary-test:{counter}")
    event_id = "evt_" + hashlib.sha256(source.encode()).hexdigest()
    text = "synthetic model boundary source"
    value = dict(authority=active["authority"], kind="authenticated_onebot_ws",
                 source_event_id=source, event_id=event_id,
                 input_sha256=input_digest({"text": text}),
                 frame_sha256="b" * 64, evidence_sha256="c" * 64, identity_sha256=identity,
                 protocol_epoch="model-boundary-test", occurred_at=core.clock(),
                 received_at=core.clock())
    value["signature"] = key.sign(capture_signing_payload(value)).hex()
    capture = OwnerCapture.model_validate(value)
    owner_input.approve(core.store, core.settings, core.clock, capture)
    incoming = event(core, text=text, origin="onebot-direct:FriendMessage:10001").model_copy(
        update={"event_id": event_id, "source_event_id": source})
    admitted = core.ingest(incoming, f"model-boundary-source-{counter}")
    original_lease = core.settings.lease_seconds
    try:
        if lease_seconds is not None:
            core.settings.lease_seconds = lease_seconds
        job = core.claim_job("model-boundary-test", job_id=admitted["job_id"])
    finally:
        core.settings.lease_seconds = original_lease
    assert job is not None
    jobs[number] = job["job_id"]
    core.authorized_model_test_jobs = jobs
    return job["job_id"]
