import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import api, input_protocol
from companion.store import encode
from companion_onebot_bridge.event_normalizer import normalize_recall
from tests.support import onebot_window as window_fixtures
from tests.support.onebot_capture import pinned_plan
from tests.support.onebot_events import raw_event
from tests.support.private_companion import companion_options


@asynccontextmanager
async def environment(core, tmp_path, monkeypatch):
    now = datetime.now(UTC)
    core.clock.now = now
    monkeypatch.setattr(window_fixtures, "raw_event", lambda: raw_event(time=int(now.timestamp())))
    config, plan = pinned_plan(core, tmp_path, 3001, live=True, approved=True,
                               scope_options=companion_options(core))
    app = api.create_app(core.settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + config.bridge_token},
    ) as client:
        opened = await client.post("/internal/v1/live-text/open",
                                   json={"window_sha256": plan.sha256})
        assert opened.status_code == 200, opened.text
        assert app.state.core is not core
        yield client, config, plan, now


def sign(config, body, domain):
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(config.capture_signing_key))
    return body | {"signature": key.sign(domain + encode(body).encode()).hex()}


def notice(config, plan, now, number=1):
    frame = encode({"time": int(now.timestamp()), "self_id": int(config.bot_qq_id),
                    "post_type": "notice", "notice_type": "friend_recall",
                    "user_id": int(config.owner_qq_id), "message_id": number})
    projection = normalize_recall(frame, config, now=now.timestamp())
    return sign(config, {
        "kind": "authenticated_onebot_friend_recall", "window_sha256": plan.sha256,
        **projection, "protocol_epoch": config.protocol_epoch,
        "identity_sha256": plan.value["identity_sha256"],
        "config_sha256": plan.value["config_sha256"], "received_at": now.isoformat(),
        "frame_sha256": hashlib.sha256(frame.encode()).hexdigest(),
        "evidence_sha256": "e" * 64,
    }, input_protocol.RECALL_DOMAIN)
