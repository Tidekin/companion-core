from datetime import timedelta
from zoneinfo import ZoneInfo

from companion import private_life


def companion_options(core):
    return {
        "automatic_reply": True,
        "companion_scope": "owner_private_v1",
        "companion_approval_sha256": "a" * 64,
        "history_scope": "owner_private",
        "history_approval_sha256": "b" * 64,
        "max_history_turns": 15,
        "max_history_utf8_bytes": 16000,
        "memory_scope": "owner_private",
        "memory_approval_sha256": "c" * 64,
        "max_model_attempts": 100,
        "window_options": {
            "ends_at": (core.clock.now + timedelta(hours=24))
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .isoformat()
        },
        "paid_options": {
            "per_request_calls": 3,
            "window_calls": 100,
            "expires_at": (core.clock.now + timedelta(days=2)).isoformat(),
            "rates_valid_until": (core.clock.now + timedelta(days=2)).isoformat(),
        },
        "core_options": {
            **{name + "_enabled": True for name in private_life.FEATURES},
            "max_recent_messages": 30,
            "extraction_min_events": 1,
            "extraction_idle_seconds": 0,
            "quiet_start": 0,
            "quiet_end": 0,
            "diary_min_source_events": 1,
            "reflection_min_sources": 1,
            "context_soft_tokens": 16000,
        },
    }
