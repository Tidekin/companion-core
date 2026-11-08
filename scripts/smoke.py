"""Launch real API and worker processes against a demo model, entirely in the workspace."""

import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.postgres_fixture import isolated_database  # noqa: E402


def main(*, scratch=None):
    root = Path(__file__).resolve().parents[1]
    scratch = Path(scratch) if scratch is not None else root / ".local/test-temp"
    scratch.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(dir=scratch, prefix="smoke-")
    with isolated_database() as database, temporary as directory:
        env = {
            **os.environ,
            "COMPANION_CONFIG": directory + "/absent.toml",
            "COMPANION_OWNER_QQ_ID": "10001",
            "COMPANION_BOT_QQ_ID": "10002",
            "COMPANION_BRIDGE_TOKEN": secrets.token_urlsafe(32),
            "COMPANION_MODE": "demo",
            "COMPANION_DATABASE": database.get_secret_value(),
            "COMPANION_STATE_DIRECTORY": directory + "/state",
            "COMPANION_EMBEDDING_CACHE_DIR": os.environ.get(
                "COMPANION_TEST_EMBEDDING_CACHE", str(root / "data/embedding-cache"),
            ),
            "COMPANION_ERASURE_LEDGER": directory + "/privacy.jsonl",
            "COMPANION_PROACTIVE_ENABLED": "false",
            "COMPANION_DIARY_ENABLED": "true",
            "COMPANION_DIARY_ADMIN_TOKEN": secrets.token_urlsafe(32),
            "COMPANION_CONTROL_TOKEN": secrets.token_urlsafe(32),
            "COMPANION_CONSOLIDATION_ENABLED": "true",
            "COMPANION_CONTENT_ENABLED": "true",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": directory,
            "TMP": directory,
        }
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.run(
            [sys.executable, "-m", "companion", "init"],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            creationflags=flags,
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        processes = []
        try:
            for args in (
                ["serve", "--port", str(port)],
                ["worker"],
                ["life-worker"],
                ["executive-worker"],
                ["content-worker"],
            ):
                processes.append(
                    subprocess.Popen(
                        [sys.executable, "-m", "companion", *args],
                        cwd=root,
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=flags,
                    )
                )
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                trust_env=False,
                timeout=3,
                headers={"Authorization": "Bearer " + env["COMPANION_BRIDGE_TOKEN"]},
            ) as client:
                for _ in range(100):
                    try:
                        if client.get("/internal/v1/health/ready").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("API failed to become ready")
                body = {
                    "schema_version": 1,
                    "event_id": "evt_smoke_001",
                    "source": "qq.onebot",
                    "source_event_id": "10002:qq:FriendMessage:10001:smoke",
                    "event_type": "message.received",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "conversation": {
                        "platform_session_key": "qq:FriendMessage:10001",
                        "scope": "private",
                    },
                    "actor": {"platform_user_id": "10001", "role": "owner"},
                    "payload": {
                        "text": "你好",
                        "media": [],
                        "shared_content": [
                            {"kind": "link", "url": "https://www.xiaoheihe.cn/app/bbs/link/123456"}
                        ],
                    },
                    "trace_id": "trace_smoke_001",
                }
                result = client.post(
                    "/internal/v1/inbox/events",
                    json=body,
                    headers={"X-Idempotency-Key": "smoke-001"},
                )
                assert result.status_code == 202
                content = client.get("/internal/v2/content/evt_smoke_001").json()
                assert content["items"][0]["error_code"] == "CONTENT_READER_UNAVAILABLE"
                assert not content["pending"]
                for _ in range(100):
                    items = client.post(
                        "/internal/v1/deliveries/lease", json={"worker_id": "smoke", "limit": 1}
                    ).json()["items"]
                    if items:
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Worker did not produce delivery")
                item = items[0]
                assert "演示模式" in item["content"]["text"]
                path = "/internal/v1/deliveries/" + item["message_id"]
                assert (
                    client.post(
                        path + "/begin-send", json={"lease_token": item["lease_token"]}
                    ).status_code
                    == 200
                )
                assert (
                    client.post(
                        path + "/result",
                        json={"lease_token": item["lease_token"], "result": "SENT"},
                    ).status_code
                    == 200
                )
                assert (
                    client.post(
                        "/internal/v1/deliveries/lease", json={"worker_id": "smoke", "limit": 1}
                    ).json()["items"]
                    == []
                )
                for _ in range(100):
                    diary = client.get(
                        "/internal/v1/diary/status",
                        headers={"Authorization": "Bearer " + env["COMPANION_DIARY_ADMIN_TOKEN"]},
                    ).json()
                    if diary.get("latest") and diary["latest"]["state"] == "SKIPPED":
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Life worker did not process empty diary days")
                assert client.get("/internal/v1/diary").status_code == 401
                for _ in range(100):
                    nightly = client.get(
                        "/internal/v2/consolidation",
                        headers={"Authorization": "Bearer " + env["COMPANION_CONTROL_TOKEN"]},
                    ).json()["items"]
                    if nightly and any(item["state"] == "SKIPPED" for item in nightly):
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Independent life worker did not process empty days")
                assert all(process.poll() is None for process in processes)
                print(
                    "PASS: API + chat/diary/life/executive/content workers "
                    "+ private delivery + auth"
                )
        finally:
            for process in processes:
                if os.name == "nt":
                    # Windows venv launchers can have a child interpreter holding the DB.
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=flags,
                        check=False,
                    )
                else:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            # Windows may retain a terminated child's file handles briefly.
            # Retry only sharing violations; a persistent lock must still fail CI.
            for attempt in range(50):
                try:
                    temporary.cleanup()
                    break
                except PermissionError as exc:
                    if getattr(exc, "winerror", None) != 32 or attempt == 49:
                        raise
                    time.sleep(0.1)


if __name__ == "__main__":
    main()
