import hashlib
from datetime import datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import paid_budget
from companion.owner_authority_protocol import OwnerCapture, OwnerGrantPolicy
from companion.qq_rich import input_digest
from companion.store import encode
from companion_delivery_protocol.authority import CAPTURE_DOMAIN
from tests.support.owner_authority import activate
from tests.support.owner_authority_setup import prepare


def captured(core, tmp_path):
    policy = prepare(core, tmp_path)
    key = Ed25519PrivateKey.generate()
    policy = OwnerGrantPolicy.model_validate({**policy.model_dump(), "purposes": ["chat"],
        "capture": {"public_key": key.public_key().public_bytes_raw().hex(),
                    "identity_sha256": paid_budget.sha(
                        [core.settings.owner_qq_id, core.settings.bot_qq_id]),
                    "protocol_epoch": "synthetic-epoch"}})
    result, _ = activate(core, policy)
    source = (f"{core.settings.bot_qq_id}:onebot-direct:FriendMessage:"
              f"{core.settings.owner_qq_id}:epoch:synthetic-epoch:123")
    value = dict(authority=result["authority"], kind="authenticated_onebot_ws",
                 source_event_id=source,
                 event_id="evt_" + hashlib.sha256(source.encode()).hexdigest(),
                 input_sha256=input_digest({"text": "synthetic owner input"}),
                 frame_sha256="b" * 64, evidence_sha256="c" * 64,
                 identity_sha256=policy.capture.identity_sha256,
                 protocol_epoch=policy.capture.protocol_epoch,
                 occurred_at=datetime.fromisoformat(core.clock()).isoformat(),
                 received_at=datetime.fromisoformat(core.clock()).isoformat())
    value["signature"] = key.sign(CAPTURE_DOMAIN + encode(value).encode()).hex()
    return OwnerCapture.model_validate(value), key


def inbound(core, capture):
    from tests.conftest import event

    message = event(core, text="synthetic owner input", origin="onebot-direct:FriendMessage:10001")
    return message.model_copy(
        update={"event_id": capture.event_id, "source_event_id": capture.source_event_id})
