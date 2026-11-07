import hashlib
from datetime import UTC, datetime

from companion.owner_authority_protocol import OwnerRecall
from companion_delivery_protocol.authority import recall_signing_payload


def receipt(core, capture, key, **changes):
    occurred = datetime.fromisoformat(core.clock()).astimezone(UTC).isoformat()
    value = dict(authority=capture.authority, kind="authenticated_onebot_friend_recall",
        recall_id="recall_" + hashlib.sha256(
            (capture.source_event_id + occurred).encode()).hexdigest(),
        target_source_event_id=capture.source_event_id, target_event_id=capture.event_id,
        protocol_epoch=capture.protocol_epoch, identity_sha256=capture.identity_sha256,
        occurred_at=occurred, received_at=occurred, frame_sha256="f" * 64,
        evidence_sha256="a" * 64)
    value.update(changes)
    value["signature"] = key.sign(recall_signing_payload(value)).hex()
    return OwnerRecall.model_validate(value)
