"""Signed WS -> real Core workers -> native FILE -> exact B2a bytes -> OneBot ACK."""

import asyncio
import base64
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from companion import meme_policy
from companion.config import ImageInputPolicy
from tests.conftest import run
from tests.meme_authority_fixture import environment
from tests.support import onebot
from tests.support.onebot import onebot_http
from tests.support.onebot_events import raw_event
from tests.test_evaluation_process import process_config
from tests.test_meme_core_process import process, until
from tests.test_meme_gallery import SELECTION, GalleryUpstream
from tests.test_meme_pack_files import RULE, Upstream


@pytest.mark.parametrize("gallery", [False, True])
def test_empty_turn_native_acquisition_caption_and_next_turn_exact_delivery(
    core, tmp_path, monkeypatch, gallery,
):
    core.settings = core.settings.model_copy(update={
        "meme_catalog_owner": "core", "meme_discovery_enabled": True,
        "meme_semantics_enabled": True, "meme_trusted_sources": [meme_policy.TrustedSource(**RULE)],
        "meme_caption_daily_micro_usd": 50000, "expression_selection_probability": 1.0,
        "embedding_cache_dir": str(tmp_path / "empty-local-weights"),
        **({"meme_gallery_source": meme_policy.GallerySource(**SELECTION),
            "meme_trusted_sources": []} if gallery else {}),
    })
    paid = {"max_output_tokens": 4096, "meme_input": ImageInputPolicy(
        approval_sha256="e" * 64, estimated_tokens_per_image=100,
        max_bytes_per_image=4000000, max_pixels_per_image=16000000,
        max_images_per_request=5)}
    sent, reports = [], []

    async def scenario():
        async with environment(core, tmp_path, monkeypatch, paid=paid) as (client, cfg, grant, _):
            second, hold = asyncio.Event(), asyncio.Event()
            ack_entered, ack_release = asyncio.Event(), threading.Event()
            loop, provider = asyncio.get_running_loop(), onebot.provider

            def paused_ack(request, sends, receipt=None):
                response = provider(request, sends, receipt)
                if request.url.path == "/send_private_msg" and len(sends) == 1:
                    loop.call_soon_threadsafe(ack_entered.set)
                    assert ack_release.wait(10), "Synthetic ACK gate was not released"
                return response

            monkeypatch.setattr(onebot, "provider", paused_ack)

            async def source(ws):
                core.clock.now = datetime.now(UTC)
                await ws.send(json.dumps(raw_event(1, text="太棒了，成功了",
                    time=int(core.clock.now.timestamp()))))
                await second.wait()
                if hold.is_set():
                    return
                core.clock.now = datetime.now(UTC)
                await ws.send(json.dumps(raw_event(2, text="想要温暖的抱抱",
                    time=int(core.clock.now.timestamp()))))
                await hold.wait()

            async def authenticate(connection, request):
                if request.headers.get("Authorization") != "Bearer " + cfg.onebot_token:
                    return connection.respond(401, "synthetic auth required")

            async def work(label, candidate="-", *, waiting=None):
                config, output = tmp_path / (label + ".json"), tmp_path / (label + "-result.json")
                if waiting is None:
                    process_config(core, config)
                    core.clock.now = datetime.now(UTC)
                    child = await process("tests.meme_native_acquisition_process", config,
                                          core.clock(), candidate, output)
                else:
                    child = waiting
                    child.stdin.write(b"run\n")
                    await child.stdin.drain()
                try:
                    async with asyncio.timeout(40):
                        _, error = await child.communicate()
                    assert child.returncode == 0, error.decode(errors="replace")
                    report = json.loads(output.read_text("utf-8"))
                    if waiting is None:
                        assert report["worked"], report
                    reports.append(report)
                    return report
                finally:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()

            with onebot_http(sent, cfg.onebot_token) as http_port:
                async with serve(source, "127.0.0.1", 0, process_request=authenticate) as ws:
                    config = replace(cfg, core_url=str(client.base_url).rstrip("/"),
                        onebot_http_url=f"http://127.0.0.1:{http_port}",
                        onebot_ws_url=f"ws://127.0.0.1:{ws.sockets[0].getsockname()[1]}",
                        owner_authority=grant.authority,
                        capture_signing_key=grant.key.private_bytes_raw().hex(),
                        data_dir=str(tmp_path / "native-bridge"), meme_enabled=True,
                        meme_catalog_owner="core")
                    config_file = tmp_path / "bridge.json"
                    config_file.write_text(json.dumps(config.snapshot()), encoding="utf-8")
                    bridge = await process("tests.native_continuous_process", config_file,
                                           stdin=asyncio.subprocess.PIPE)
                    waiting = None
                    try:
                        first = await until(lambda: core.store.one(
                            "SELECT * FROM jobs WHERE kind='chat' AND state='QUEUED'"))
                        # Preload the independent idle worker before pausing the real HTTP ACK.
                        waiting_config = tmp_path / "waiting.json"
                        process_config(core, waiting_config)
                        waiting = await process("tests.meme_native_acquisition_process",
                            waiting_config, core.clock(), "-", tmp_path / "waiting-result.json",
                            "gate", stdin=asyncio.subprocess.PIPE)
                        async with asyncio.timeout(40):
                            assert (await waiting.stdout.readline()).strip() == b"READY"
                        assert (await work("empty-chat"))["calls"] == ["chat"]
                        async with asyncio.timeout(10):
                            await ack_entered.wait()
                        assert core.store.one("SELECT state FROM jobs WHERE job_id=?",
                            (first["job_id"],))["state"] == "SUCCEEDED"
                        delivery_before = core.store.rows("SELECT * FROM outbox")
                        assert delivery_before[0]["state"] == "SENDING"
                        discovery_before = core.store.one(
                            "SELECT * FROM jobs WHERE kind='meme_discovery'")
                        metadata_before = core.store.one(
                            "SELECT value FROM runtime_meta WHERE key=?",
                            ("meme_discovery:" + discovery_before["job_id"],))
                        accounting_before = {table: core.store.rows("SELECT * FROM " + table)
                            for table in ("model_usage", "model_admissions")}
                        paused = await work("waiting", waiting=waiting)
                        assert paused["requests"] == 0 and paused["calls"] == []
                        after_wait = core.store.one("SELECT * FROM jobs WHERE job_id=?",
                            (discovery_before["job_id"],))
                        assert after_wait == discovery_before, {
                            "discovery": after_wait,
                            "reservations": core.store.rows(
                                "SELECT * FROM meme_discovery_reservations"),
                            "worker": paused,
                        }
                        assert not paused["worked"]
                        assert not core.store.rows("SELECT * FROM meme_discovery_reservations")
                        assert core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                            ("meme_discovery:" + discovery_before["job_id"],)) == metadata_before
                        assert core.store.rows("SELECT * FROM outbox") == delivery_before
                        assert {table: core.store.rows("SELECT * FROM " + table)
                            for table in accounting_before} == accounting_before
                        ack_release.set()
                        await until(lambda: core.store.one(
                            "SELECT 1 FROM outbox WHERE state='SENT'"))
                        frozen = core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                            ("meme_offer:" + first["event_id"],))["value"]
                        assert json.loads(frozen)["offer"]["candidates"] == []
                        assert len(sent) == 1 and len(sent[0]["message"]) == 1
                        acquired = await work("acquire")
                        assert acquired["calls"] == []
                        assert acquired["requests"] == (2 if gallery else 5)
                        reservation = core.store.one("SELECT * FROM meme_discovery_reservations")
                        assert reservation["job_id"] == discovery_before["job_id"]
                        assert len(core.store.rows(
                            "SELECT * FROM jobs WHERE kind='meme_discovery'")) == 1
                        assert core.store.one("SELECT attempts FROM jobs WHERE job_id=?",
                            (discovery_before["job_id"],))["attempts"] == 1
                        asset = core.store.one("SELECT * FROM meme_assets")
                        assert reservation["state"] == "SETTLED"
                        assert reservation["download_bytes"] == acquired["download_bytes"]
                        assert reservation["storage_bytes"] == len(asset["data"])
                        assert asset["state"] == "STAGED"
                        assert (await work("caption"))["calls"] == ["caption"]
                        caption = core.store.one("SELECT * FROM meme_captions")
                        assert caption["result_json"]
                        assert core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                            ("meme_offer:" + first["event_id"],))["value"] == frozen
                        second.set()
                        last = await until(lambda: core.store.one(
                            "SELECT * FROM jobs WHERE kind='chat' AND state='QUEUED'"))
                        assert (await work("next-chat", asset["asset_sha256"]))["calls"] == ["chat"]
                        await until(lambda: core.store.one(
                            "SELECT 1 FROM outbox WHERE origin_event_id=? AND state='SENT'",
                            (last["event_id"],)))
                        assert len(sent) == 2 and len(sent[1]["message"]) == 2
                        data = base64.b64decode(sent[1]["message"][1]["data"]["file"]
                                                .removeprefix("base64://"))
                        assert data == asset["data"]
                        assert data == (GalleryUpstream().image if gallery else
                                        next(iter(Upstream(count=1).images.values())))
                        delivery = core.store.one("SELECT message_id FROM outbox "
                            "WHERE origin_event_id=?", (last["event_id"],))
                        binding = json.loads(core.store.one("SELECT value FROM runtime_meta "
                            "WHERE key=?", ("owner_delivery:" + delivery["message_id"],))["value"])
                        assert binding["meme_binding_version"] == 2
                        assert core.store.one("SELECT 1 FROM job_sources "
                            "WHERE job_id=? AND event_id=?",
                            (last["job_id"], first["event_id"]))

                        def acknowledged():
                            with closing(sqlite3.connect((Path(config.data_dir) / "spool.sqlite3")
                                    .resolve().as_uri() + "?mode=ro", uri=True)) as db:
                                return db.execute("SELECT COUNT(*) FROM window_attempts "
                                                  "WHERE state='ACK'").fetchone()[0] == 2

                        await until(acknowledged)
                        bridge.stdin.write(b"stop\n")
                        await bridge.stdin.drain()
                        async with asyncio.timeout(15):
                            output, error = await bridge.communicate()
                        assert bridge.returncode == 0, error.decode(errors="replace")
                        assert json.loads(output.decode().strip().splitlines()[-1])["attempts"] == {
                            "ACK": 2}
                    finally:
                        ack_release.set()
                        hold.set()
                        second.set()
                        if waiting is not None and waiting.returncode is None:
                            waiting.kill()
                            await waiting.wait()
                        if bridge.returncode is None:
                            bridge.kill()
                            await bridge.wait()
            usage = core.store.rows("SELECT scenario,state FROM model_usage ORDER BY rowid")
            assert len(usage) == 3 and all(u["state"] == "SETTLED" for u in usage)
            assert len([c for r in reports for c in r["calls"] if c == "caption"]) == 1
            assert sum(r["requests"] for r in reports) == (2 if gallery else 5)
            assert not core.store.rows("SELECT 1 FROM outbox WHERE state='DELIVERY_UNCERTAIN'")
            with sqlite3.connect(Path(config.data_dir) / "spool.sqlite3") as db:
                tables = {r[0] for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                assert not tables & {"meme_assets", "meme_offers", "meme_caption_cache",
                                     "meme_semantic_vectors", "meme_runtime_sources"}
                assert db.execute("SELECT state FROM meme_pins").fetchone()[0] == "SENT"

    run(scenario())
