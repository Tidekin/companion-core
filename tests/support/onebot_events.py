"""Synthetic owner-private OneBot events and Bridge configuration."""

import json
from datetime import UTC, datetime

from companion_onebot_bridge.config import BridgeConfig
from companion_onebot_bridge.event_normalizer import normalize


def config(**changes):
    return BridgeConfig(
        **{
            "owner_qq_id": "10001",
            "bot_qq_id": "10002",
            "bridge_token": "synthetic-core-token-32-characters",
            "onebot_token": "synthetic-onebot-token-32-characters",
            "protocol_epoch": "offline-first-20261020",
            **changes,
        }
    )


def raw_event(number=42, text="你好", **changes):
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "self_id": 10002,
        "user_id": 10001,
        "sender": {"user_id": 10001},
        "message_id": number,
        "time": 1792382400,
        "message": [{"type": "text", "data": {"text": text}}],
        **changes,
    }


def normalized(raw=None, cfg=None):
    return normalize(
        json.dumps(raw or raw_event(), ensure_ascii=False),
        cfg or config(),
        now=datetime(2026, 10, 20, tzinfo=UTC).timestamp(),
    )
