"""Real signed ingress/API, independent Core worker and actual Bridge daemon, loopback only."""

import asyncio
import base64
import json
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from companion.core import Core
from tests.conftest import run
from tests.native_continuous_support import until as wait_for_evidence
from tests.support.onebot import onebot_http
from tests.support.onebot_events import raw_event
from tests.test_evaluation_process import process_config
from tests.test_meme_core_runtime import runtime_case as runtime_case
from tests.test_native_meme_host import worker
from tests.test_onebot_capture import real_core_http

ROOT = Path(__file__).resolve().parents[1]


async def until(predicate, *, seconds=20):
    await wait_for_evidence(predicate, deadline_seconds=seconds)
    return predicate()


async def process(module, *args, stdin=None):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-m",
        module,
        *map(str, args),
        cwd=ROOT,
        env=env,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


class RealClock:
    @property
    def now(self):
        return datetime.now(UTC)

    def __call__(self):
        return self.now.isoformat()


@pytest.mark.parametrize("runtime_case", ["no_current"], indirect=True)
def test_signed_core_worker_and_bridge_process_use_one_original_image(runtime_case, tmp_path):
    c = runtime_case
    # This test intentionally requires the real Settings and Worker integration.
    # It must not be made green by a test-side monkeypatch of the offer hooks.
    assert "meme_catalog_owner" in type(c.core.settings).model_fields
    c.core.clock = RealClock()
    sent, children = [], []
    original_usage = c.core.store.rows("SELECT * FROM model_usage")

    async def scenario():
        connected = asyncio.Event()
        hold = asyncio.Event()

        async def source(ws):
            await ws.send(
                json.dumps(
                    raw_event(2, text="想要温暖的抱抱", time=int(c.core.clock.now.timestamp()))
                )
            )
            connected.set()
            await hold.wait()

        async def authenticate(connection, request):
            if request.headers.get("Authorization") != "Bearer " + c.config.onebot_token:
                return connection.respond(401, "synthetic auth required")

        with Core(c.core.settings, clock=c.core.clock) as api_core:
            with onebot_http(sent, c.config.onebot_token) as http_port:
                async with (
                    real_core_http(api_core) as api_port,
                    serve(
                        source,
                        "127.0.0.1",
                        0,
                        process_request=authenticate,
                    ) as ws,
                ):
                    config = replace(
                        c.config,
                        core_url=f"http://127.0.0.1:{api_port}",
                        onebot_http_url=f"http://127.0.0.1:{http_port}",
                        onebot_ws_url=f"ws://127.0.0.1:{ws.sockets[0].getsockname()[1]}",
                        owner_authority=c.grant.authority,
                        capture_signing_key=c.grant.key.private_bytes_raw().hex(),
                        data_dir=str(tmp_path / "core-transport"),
                        meme_enabled=True,
                        meme_catalog_owner="core",
                    )
                    config_file = tmp_path / "bridge.json"
                    config_file.write_text(json.dumps(config.snapshot()), "utf-8")
                    bridge = await process(
                        "tests.native_continuous_process",
                        config_file,
                        stdin=asyncio.subprocess.PIPE,
                    )
                    children.append(bridge)
                    try:
                        async with asyncio.timeout(15):
                            await connected.wait()
                        job = await until(
                            lambda: c.core.store.one(
                                "SELECT * FROM jobs WHERE kind='chat' AND state='QUEUED'"
                            )
                        )
                        await worker(c.core, tmp_path, c.item["asset_sha256"])
                        row = await until(
                            lambda: c.core.store.one("SELECT * FROM outbox WHERE state='SENT'")
                        )
                        assert row["origin_event_id"] == job["event_id"]
                        assert len(sent) == 1
                        assert (
                            base64.b64decode(
                                sent[0]["message"][1]["data"]["file"].removeprefix("base64://")
                            )
                            == c.data
                        )
                        assert c.core.store.one(
                            "SELECT 1 FROM job_sources WHERE job_id=? AND event_id=?",
                            (job["job_id"], c.source["event_id"]),
                        )
                        assert (
                            json.loads(
                                c.core.store.one(
                                    "SELECT value FROM runtime_meta WHERE key=?",
                                    ("owner_delivery:" + row["message_id"],),
                                )["value"]
                            )["meme_binding_version"]
                            == 2
                        )

                        def locally_acknowledged():
                            database = Path(config.data_dir) / "spool.sqlite3"
                            with closing(
                                sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
                            ) as db:
                                return db.execute(
                                    "SELECT 1 FROM window_attempts WHERE state='ACK'"
                                ).fetchone()

                        await until(locally_acknowledged)
                        bridge.stdin.write(b"stop\n")
                        await bridge.stdin.drain()
                        async with asyncio.timeout(15):
                            output, error = await bridge.communicate()
                        assert bridge.returncode == 0, error.decode(errors="replace")
                        report = json.loads(output.decode().strip().splitlines()[-1])
                        assert report["attempts"] == {"ACK": 1}
                    finally:
                        hold.set()
                        for child in children:
                            if child.returncode is None:
                                child.kill()
                                await child.wait()
                    with sqlite3.connect(Path(config.data_dir) / "spool.sqlite3") as db:
                        tables = {
                            r[0]
                            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                        }
                        assert not tables & {
                            "meme_assets",
                            "meme_offers",
                            "meme_caption_cache",
                            "meme_semantic_vectors",
                            "meme_runtime_sources",
                        }
                        assert db.execute("SELECT state FROM meme_pins").fetchone()[0] == "SENT"
        usage = c.core.store.rows("SELECT * FROM model_usage ORDER BY rowid")
        assert len(usage) == len(original_usage) + 1 and usage[0] == original_usage[0]
        assert usage[-1]["state"] == "SETTLED"
        assert not c.core.store.rows("SELECT * FROM outbox WHERE state='DELIVERY_UNCERTAIN'")

    run(scenario())


def test_original_root_withdrawal_at_actual_model_boundary_never_calls_provider(
    runtime_case, tmp_path
):
    c = runtime_case
    assert "meme_catalog_owner" in type(c.core.settings).model_fields
    # The original test claimed a native lease to exercise domain preparation;
    # no model reservation or I/O has begun. Let the actual independent Worker claim it.
    c.core.store.execute(
        "UPDATE jobs SET state='QUEUED',lease_token=NULL WHERE job_id=?", (c.job["job_id"],)
    )
    config = tmp_path / "model-boundary.json"
    process_config(c.core, config)
    before = c.core.store.rows("SELECT * FROM model_usage")

    async def scenario():
        child = await process(
            "tests.owner_runtime_process",
            "--config",
            config,
            "--now",
            c.core.clock(),
            "--fence",
            stdin=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(25):
                marker = await child.stdout.readline()
            if not marker:
                _, error = await child.communicate()
                raise AssertionError(error.decode(errors="replace"))
            assert marker.strip() == b"BEFORE_DISPATCH", marker
            with c.core.store.transaction():
                c.core.ledger.expire_events(c.core.store, [c.source["event_id"]], c.core.clock())
            child.stdin.write(b"continue\n")
            await child.stdin.drain()
            async with asyncio.timeout(20):
                output, error = await child.communicate()
            assert child.returncode == 0, error.decode(errors="replace")
            report = json.loads(output.decode().strip().splitlines()[-1])
            assert report["worked"] and report["calls"] == 0
            assert not c.core.store.rows("SELECT * FROM outbox")
            assert (
                c.core.store.one(
                    "SELECT * FROM model_usage WHERE request_id=?", (before[0]["request_id"],)
                )
                == before[0]
            )
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

    run(scenario())
