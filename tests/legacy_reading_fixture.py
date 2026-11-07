"""Frozen v1 capture fixture retained for the legacy video contract tests."""

import hashlib

from companion import live_text
from companion.store import encode
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.rich_input import input_digest
from tests.support.input_protocol import sign
from tests.support.onebot_events import raw_event


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
    capture = sign(config, {
        "kind": "authenticated_onebot_ws", "window_sha256": plan.sha256,
        "event_id": event["event_id"], "source_event_id": event["source_event_id"],
        "input_sha256": input_digest(event["payload"]),
        "frame_sha256": hashlib.sha256(encode(raw).encode()).hexdigest(),
        "evidence_sha256": "e" * 64, "identity_sha256": plan.value["identity_sha256"],
        "config_sha256": plan.value["config_sha256"], "protocol_epoch": config.protocol_epoch,
        "received_at": now.isoformat(), "occurred_at": event["occurred_at"],
    }, live_text.CAPTURE_SIGNATURE_DOMAIN)
    approved = await client.post("/internal/v1/live-text/inputs/approve", json={
        "window_sha256": plan.sha256, "event_id": event["event_id"],
        "input_sha256": capture["input_sha256"], "evidence_sha256": "f" * 64,
        "capture": capture,
    })
    assert approved.status_code == 200, approved.text
    accepted = await client.post("/internal/v1/inbox/events", json=event,
                                headers={"X-Idempotency-Key": event["event_id"]})
    assert accepted.status_code == 202, accepted.text
    return event, accepted.json()

