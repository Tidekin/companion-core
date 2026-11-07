"""Explicit account-authorized samples through real Core HTTP/storage and erasure.

Never run in CI. The model/QQ sender are simulated; sources and ASGI/SQLite/backup
are real. Only redacted counts, hashes and source references leave the temporary DB.
"""

import argparse
import asyncio
import hashlib
import json
import secrets
import time
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

from companion.api import create_app
from companion.backup import backup, restore
from companion.config import Settings
from companion.core import Core
from companion.store import Store, migrate
from source_reader.accounts import BILIBILI_COMMIT, HEYBOX_COMMIT
from source_reader.bili_tools import BilibiliAccountHTTP, BilibiliToolReader
from source_reader.heybox import HeyboxReader
from source_reader.heybox_source import NativeHeyboxSource
from source_reader.worker import read_result
from tests.conftest import FakeModel, event
from tests.support.exploration import configure

ROOT = Path(__file__).resolve().parents[1]

class WallClock:
    def __init__(self):
        self.offset = timedelta()

    def __call__(self):
        return (datetime.now(UTC) + self.offset).isoformat()


async def verify(args):
    if not 1 <= len(args.urls) <= 10:
        raise ValueError("Provide 1–10 explicitly selected source URLs")
    data = args.data_dir.resolve()
    if not data.is_relative_to(ROOT / ".local/test-temp") or data.exists():
        raise ValueError("Use a new directory under .local/test-temp")
    data.mkdir(parents=True)
    settings = Settings(
        owner_qq_id="10001",
        bot_qq_id="10002",
        bridge_token=secrets.token_urlsafe(32),
        database=str(data / "core.db"),
        erasure_ledger=str(data / "erasure.jsonl"),
        proactive_enabled=False,
    )
    migrate(settings.database)
    model, clock = FakeModel(), WallClock()
    core = Core(settings, model, clock)
    configure(core)
    settings.content_enabled = True
    settings.exploration_daily_requests = settings.exploration_monthly_requests = 50
    settings.exploration_daily_deep_reads = len(args.urls)
    adapter = "heybox" if args.source == "heybox" else "bili"
    settings.observation_adapters[adapter].reader_max_requests = 1 if adapter == "heybox" else 4
    app = create_app(settings)
    app.state.core = core
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "source": args.source,
        "upstream_commit": HEYBOX_COMMIT if adapter == "heybox" else BILIBILI_COMMIT,
        "credentials_used": True,
        "authorization": "owner_interactive_login_in_this_task",
        "real_model_calls": 0,
        "qq_messages": 0,
        "samples": [],
        "core_transport": "actual_ASGI_HTTP_and_SQLite",
        "production_ready": False,
    }
    started = time.monotonic()
    roots = []
    try:
        async with AsyncExitStack() as stack:
            if adapter == "heybox":
                source = await stack.enter_async_context(NativeHeyboxSource(args.account_file))
                reader = HeyboxReader(None, source)
            else:
                source = await stack.enter_async_context(BilibiliAccountHTTP(args.account_file))
                reader = BilibiliToolReader(source)
            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url="http://core",
                    transport=httpx.ASGITransport(app=app),
                    trust_env=False,
                )
            )
            bridge_headers = {
                "Authorization": "Bearer " + settings.bridge_token.get_secret_value(),
                "X-Idempotency-Key": "",
            }
            adapter_headers = {
                "Authorization": "Bearer "
                + settings.observation_adapters[adapter].token.get_secret_value()
            }
            for index, url in enumerate(args.urls, 1):
                incoming = event(core, index, url)
                bridge_headers["X-Idempotency-Key"] = "p3-live-share-" + str(index)
                response = await http.post(
                    "/internal/v1/inbox/events",
                    headers=bridge_headers,
                    json=incoming.model_dump(mode="json"),
                )
                response.raise_for_status()
                roots.append(incoming.event_id)
                prefix = "/internal/v2/readers/" + adapter
                response = await http.post(prefix + "/lease", headers=adapter_headers)
                response.raise_for_status()
                lease = response.json()["item"]
                assert lease and lease["url"] == url
                result = await read_result(reader, lease)
                response = await http.post(
                    prefix + "/" + lease["request_id"] + "/result",
                    headers=adapter_headers,
                    json=result,
                )
                response.raise_for_status()
                repeated = await http.post(
                    prefix + "/" + lease["request_id"] + "/result",
                    headers=adapter_headers,
                    json=result,
                )
                repeated.raise_for_status()
                assert repeated.json()["duplicate"]
                context = core.content.context_message(incoming.event_id, 16000)
                sample = {
                    "url": url,
                    "status": result["status"],
                    "error_code": result.get("error_code"),
                    "used_requests": result["used_requests"],
                    "used_bytes": result["used_bytes"],
                    "receipt_idempotent": True,
                }
                if result["items"]:
                    item = dict(result["items"][0])
                    text = item.pop("text")
                    first = text.split("\n字幕：\n")[-1].splitlines()[0][:60]
                    sample.update(
                        item,
                        text_chars=len(text),
                        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                        context_has_source_text=json.dumps(first, ensure_ascii=False)[1:-1]
                        in context,
                        external_untrusted="external_untrusted" in context,
                    )
                    assert sample["context_has_source_text"] and sample["external_untrusted"]
                report["samples"].append(sample)
                print(
                    json.dumps(
                        {
                            "url": url,
                            "status": result["status"],
                            "error_code": result.get("error_code"),
                        }
                    ),
                    flush=True,
                )
                if result.get("error_code") in {
                    "UPSTREAM_ACCESS_LIMIT",
                    "UPSTREAM_FORBIDDEN",
                    "UPSTREAM_RATE_LIMITED",
                    "UPSTREAM_CAPTCHA_REQUIRED",
                    "SOURCE_CREDENTIAL_REVOKED",
                    "SOURCE_DISABLED",
                }:
                    report["stopped_on_source_gate"] = True
                    break
                await asyncio.sleep(1)
        clock.offset = timedelta(seconds=31)
        assert await core.run_once()
        report["mock_chat_calls"] = sum(scenario == "chat" for _, scenario in model.calls)
        report["outbox_rows_not_sent"] = len(core.store.rows("SELECT * FROM outbox"))
        report["observations_fetched"] = core.store.one(
            "SELECT COUNT(*) AS n FROM observations WHERE result_status='FETCHED'"
        )["n"]
        key, archive = Fernet.generate_key(), data / "old.enc"
        backup(settings.database, archive, key)
        with core.store.transaction():
            core.ledger.expire_events(core.store, roots, clock())
        restore(archive, data / "restored.db", key, settings.erasure_ledger)
        restored = Store(str(data / "restored.db"))
        try:
            assert all(
                row["redacted"] and row["evidence_json"] == "{}"
                for row in restored.rows("SELECT * FROM observations")
            )
            assert all(
                row["redacted"] and not row["target_url"]
                for row in restored.rows("SELECT * FROM content_items")
            )
            assert not restored.rows("PRAGMA foreign_key_check")
            report["encrypted_restore_latest_erasure_replay"] = "PASS"
        finally:
            restored.close()
        # The retained archive has no retained key and the current/restored DBs are erased.
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        core.store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--source", choices=["heybox", "bilibili"], required=True)
    parser.add_argument("--account-file", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(verify(parser.parse_args()))
