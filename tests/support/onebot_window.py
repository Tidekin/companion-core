from datetime import UTC, datetime, timedelta

from companion_onebot_bridge.window import digest
from tests.support.onebot_events import config, normalized, raw_event


def plan_fixture(tmp_path, **changes):
    cfg = config(
        data_dir=str(tmp_path / "spool"),
        core_url="http://127.0.0.1:8765",
        onebot_http_url="http://127.0.0.1:3000",
        onebot_ws_url="ws://127.0.0.1:3001",
    )
    now = datetime.fromtimestamp(raw_event()["time"], UTC)
    inventory = {
        "identity_sha256": digest([cfg.owner_qq_id, cfg.bot_qq_id]),
        "protocol_epoch": cfg.protocol_epoch,
        "config_sha256": digest(cfg.snapshot()),
        "config_version": "SYNTHETIC-ONLY-1",
        "image_ids": {
            name: "sha256:" + digit * 64
            for name, digit in (("core", "a"), ("bridge", "b"), ("protocol", "c"))
        },
    }
    value = {
        "window_id": "win_" + "1" * 32,
        "timezone": "Asia/Shanghai",
        "starts_at": (now - timedelta(seconds=60))
        .astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai"))
        .isoformat(),
        "ends_at": (now + timedelta(minutes=30))
        .astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai"))
        .isoformat(),
        **inventory,
        "runtime_attestation_sha256": digest(inventory),
        "authorization_sha256": "b" * 64,
        "journal_path": str(tmp_path / "window-audit.jsonl"),
        "max_new_inputs": 5,
        "max_attempts": 5,
        "max_visible_replies": 5,
        "concurrency": 1,
        "max_input_chars": 500,
        "max_reply_chars": 500,
        "execution": "local_synthetic",
        **changes,
    }
    return cfg, value, inventory, now


def input_approval(plan, raw):
    incoming = normalized(raw, plan.config)
    return {
        "window_sha256": plan.sha256,
        "event_id": incoming["event_id"],
        "input_sha256": digest(incoming["payload"]["text"]),
        "evidence_sha256": "e" * 64,
    }


def reply_approval(plan, core):
    row = core.store.one('SELECT * FROM outbox ORDER BY record_sequence DESC LIMIT 1')
    return {
        "window_sha256": plan.sha256,
        "event_id": row["origin_event_id"],
        "message_id": row["message_id"],
        "reply_sha256": digest(__import__("json").loads(row["content_json"])["text"]),
        "evidence_sha256": "f" * 64,
    }


async def stage(window, http, core, number=1, **changes):
    raw = raw_event(number, **changes)
    await window.ingest(http, raw, input_approval(window.plan, raw))
    assert await core.run_once("synthetic-window")
    return reply_approval(window.plan, core)
