import hashlib

from companion import live_text
from companion.store import encode
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.rich_input import input_digest
from tests.support.input_protocol import sign
from tests.support.onebot_events import raw_event


async def approve_only(client, config, plan, now):
    raw = raw_event(1, time=int(now.timestamp()), text="撤回发生在入箱之前")
    event = normalize(encode(raw), config, now=now.timestamp())
    capture = sign(config, {
        "kind": "authenticated_onebot_ws", "window_sha256": plan.sha256,
        "event_id": event["event_id"], "source_event_id": event["source_event_id"],
        "input_sha256": input_digest(event["payload"]),
        "frame_sha256": hashlib.sha256(encode(raw).encode()).hexdigest(),
        "evidence_sha256": "e" * 64, "identity_sha256": plan.value["identity_sha256"],
        "config_sha256": plan.value["config_sha256"], "protocol_epoch": config.protocol_epoch,
        "received_at": now.isoformat(), "occurred_at": event["occurred_at"],
    }, live_text.CAPTURE_SIGNATURE_DOMAIN)
    response = await client.post("/internal/v1/live-text/inputs/approve", json={
        "window_sha256": plan.sha256, "event_id": event["event_id"],
        "input_sha256": capture["input_sha256"], "evidence_sha256": "f" * 64,
        "capture": capture,
    })
    assert response.status_code == 200, response.text
    return event
