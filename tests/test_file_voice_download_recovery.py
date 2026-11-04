"""Native file/voice download failures keep custody and allow the next owner input."""

import asyncio
import json
import pickle
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.server import serve

from companion import content_files, image_spool, owner_authority, qq_files
from companion.core import Core
from companion.owner_authority_protocol import AuthorityStop
from companion.store import after, encode
from companion_onebot_bridge import app, pending_media
from companion_onebot_bridge.metrics import Metrics
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.onebot_client import OneBotClient
from tests.conftest import run
from tests.native_continuous_support import until
from tests.sdk_reader_fixture import answer
from tests.support.onebot import provider
from tests.support.onebot_events import raw_event
from tests.test_native_file_pipeline import configure
from tests.test_native_image_pipeline import admit_image
from tests.test_onebot_voice_media import URL as VOICE_URL
from tests.test_onebot_voice_media import segment as voice_segment
from tests.test_onebot_voice_pipeline import fixture as voice_fixture
from tests.test_prepare_owner_input import native_spool, window


async def claim_file(core, client, config, plan, tmp_path):
    event, _ = await admit_image(core, client, config, plan, tmp_path, message=[
        {"type": "file", "data": {"file_id": "expired-file", "file": "notes.txt",
                                  "file_size": 12}}])
    original = event["payload"]["rich"]["segments"][0]
    response = await client.post(f"/internal/v2/content/{event['event_id']}/file/claim", json={
        "request_key": "file:0", "source_sha256": original["source_sha256"]})
    assert response.status_code == 200, response.text
    return event, response.json()


@pytest.mark.parametrize("stopped", [False, True])
def test_expired_native_file_download_keeps_original_lease_and_traffic(core, tmp_path, stopped):
    async def scenario():
        configure(core)
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, lease = await claim_file(core, client, config, plan, tmp_path)
            before = core.store.one("SELECT * FROM content_items")
            if stopped:
                owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                    operation_id="stop-expired-file-download", authority=plan.authority))
            core.clock.advance(21)
            with Core(core.settings, core.model, core.clock) as worker:
                assert worker.store is not core.store
                assert await content_files.run_file_once(worker.content)
                assert not await content_files.run_file_once(worker.content)
            saved = core.store.one("SELECT * FROM content_items")
            assert saved["state"] == "FAILED"
            assert saved["error_code"] == "PRIVATE_FILE_DOWNLOAD_EXPIRED"
            retained = ("lease_token", "lease_until", "reserved_bytes")
            assert {key: saved[key] for key in retained} == {key: before[key] for key in retained}
            assert saved["lease_token"] == lease["lease_token"] and saved["reserved_bytes"] > 0
            assert not core.store.rows("SELECT * FROM observations")
            assert not core.store.rows("SELECT * FROM model_usage")
            retry = await client.post(f"/internal/v2/content/{event['event_id']}/file/claim",
                json={"request_key": "file:0",
                      "source_sha256": event["payload"]["rich"]["segments"][0]["source_sha256"]})
            assert retry.status_code in {403, 409, 410}
    run(scenario())


@pytest.mark.parametrize("evidence", ["raw", "staged", "usage", "signature", "source",
                                      "snapshot", "authority", "unknown", "running"])
def test_ambiguous_file_expiry_never_becomes_a_proven_download_failure(core, tmp_path, evidence):
    async def scenario():
        configure(core)
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, _ = await claim_file(core, client, config, plan, tmp_path)
            row = core.store.one("SELECT * FROM content_items")
            if evidence == "raw":
                image_spool.put(core.store, row, b"partial body", "text/plain",
                                after(core.clock(), 60))
            elif evidence == "staged":
                core.store.insert("runtime_meta", key=qq_files.STAGED + row["item_id"], value="{}")
            elif evidence == "usage":
                core.store.execute("UPDATE content_items SET usage_request_id='model_uncertain'")
            elif evidence == "signature":
                key = "owner_input:" + event["event_id"]
                receipt = json.loads(core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                                                    (key,))["value"])
                receipt["capture"]["signature"] = "0" * 128
                core.store.execute("UPDATE runtime_meta SET value=? WHERE key=?",
                                   (encode(receipt), key))
            elif evidence == "source":
                body = event["payload"] | {"text": "changed after capture"}
                core.store.execute("UPDATE events SET body_json=? WHERE event_id=?",
                                   (encode(body), event["event_id"]))
            elif evidence == "snapshot":
                snapshot = json.loads(row["attachment_json"]) | {"actual_bytes": 1}
                core.store.execute("UPDATE content_items SET attachment_json=?",
                                   (encode(snapshot),))
            elif evidence == "authority":
                core.store.execute("UPDATE jobs SET authority_json=? WHERE kind='chat'",
                                   (encode(plan.authority | {"policy_sha256": "f" * 64}),))
            else:
                core.store.execute("UPDATE content_items SET state=?,"
                    "error_code='PRIVATE_FILE_PROCESSOR_LOST'", (evidence.upper(),))
            original = core.store.one("SELECT * FROM content_items")
            core.clock.advance(21)
            assert not content_files.expire_downloads(core.content)
            assert core.store.one("SELECT * FROM content_items") == original
            if original["state"] == "FETCHING":
                assert await content_files.run_file_once(core.content)
                saved = core.store.one("SELECT * FROM content_items")
                assert saved["state"] == "UNKNOWN"
                for key in ("lease_token", "lease_until", "reserved_bytes", "usage_request_id",
                            "media_sha256", "fingerprint", "attachment_json"):
                    assert saved[key] == original[key]
                if evidence == "staged":
                    assert core.store.one("SELECT value FROM runtime_meta WHERE key=?",
                                          (qq_files.STAGED + row["item_id"],))["value"] == "{}"
                # Existing privacy cleanup can remove UNKNOWN bytes, but the
                # retained uncertainty must never later turn into a failed GET.
                image_spool.cleanup(core.store, core.clock())
                assert not content_files.expire_downloads(core.content)
                assert core.store.one("SELECT state FROM content_items")["state"] == "UNKNOWN"
            assert not core.store.rows("SELECT * FROM model_usage")
            assert not core.store.rows("SELECT * FROM observations")
    run(scenario())


async def sdk_reply(core, tmp_path, number, text):
    job = core.store.one("SELECT * FROM jobs WHERE kind='chat' ORDER BY rowid DESC LIMIT 1")
    args, output = tmp_path / f"fallback-{number}.pickle", tmp_path / f"fallback-{number}.json"
    args.write_bytes(pickle.dumps((core.settings, core.clock.now, job["job_id"],
                                  [answer(text)], "SUCCEEDED", str(output))))
    child = await asyncio.create_subprocess_exec(sys.executable, "-B",
        str(Path(__file__).with_name("reader_sdk_worker.py")), str(args),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        log, _ = await asyncio.wait_for(child.communicate(), 45)
        assert child.returncode == 0, log.decode(errors="replace")
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    return json.loads(output.read_text(encoding="utf-8"))


@pytest.mark.parametrize("kind", ["file", "voice"])
def test_native_download_failure_keeps_receiver_alive_and_reaches_independent_worker(
    core, tmp_path, kind,
):
    frames = asyncio.Queue()
    attempted = asyncio.Event()
    downloads, lookups = [], []

    async def source(ws):
        async def send():
            while True:
                await ws.send(await frames.get())
        sender = asyncio.create_task(send())
        try:
            await ws.wait_closed()
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    async def download(request):
        downloads.append(request)
        attempted.set()
        raise httpx.ConnectError("synthetic media download failed", request=request)

    async def resolve(*_):
        return ["8.8.8.8"]

    def native(request):
        lookups.append(request.url.path)
        if request.url.path == "/get_private_file_url":
            return httpx.Response(200, json={"status": "ok", "retcode": 0,
                "data": {"url": "https://files.example.test/expired"}})
        return provider(request, [])

    async def exercise(runtime, client, protocol):
        # Startup can outlast the receiver's two-second custody clock bound.
        core.clock.now = datetime.now(UTC)
        stop, wake = asyncio.Event(), asyncio.Event()
        metrics = Metrics("OFFLINE_FIXTURE", lambda _: None)
        onebot = OneBotClient(runtime.config, protocol)
        media = pending_media.MediaPump(runtime, client, protocol=onebot,
            file_transport=httpx.MockTransport(download), file_resolver=resolve,
            voice_transport=httpx.MockTransport(download), voice_resolver=resolve)
        tasks = [asyncio.create_task(app.receive(runtime.config, runtime.spool, wake, stop,
                    metrics, onebot, runtime=runtime, core=client)),
                 asyncio.create_task(app.upload(client, runtime.spool, wake, stop, metrics,
                                                runtime=runtime)),
                 asyncio.create_task(app.transfer_media(media, stop))]
        try:
            segment = ({"type": "file", "data": {"file_id": "unavailable-file",
                "file": "notes.txt", "file_size": 12}} if kind == "file" else voice_segment())
            await frames.put(encode(raw_event(1, time=int(datetime.now(UTC).timestamp()), message=[
                {"type": "text", "data": {"text": "附件读不到也请保留这段原文"}}, segment])))
            await until(attempted.is_set, tasks=tasks, deadline_seconds=20)
            core.clock.advance(21)
            if kind == "file":
                with Core(core.settings, core.model, core.clock) as worker:
                    assert await content_files.run_file_once(worker.content)
            await until(lambda: runtime.db.execute(
                "SELECT state FROM automatic_media").fetchone()[0] == "DONE", tasks=tasks)
            assert len(downloads) == 1 and all(not task.done() for task in tasks)
            assert runtime.row()["state"] == "ACTIVE"
            assert not core.store.rows("SELECT * FROM voice_jobs")
            requests = await sdk_reply(core, tmp_path, 1, "附件暂时读不到，我收到了这段原文。")
            assert requests[0]["messages"][-1]["content"] == "附件读不到也请保留这段原文"
            wire = encode(requests[0]["messages"])
            assert ("PRIVATE_FILE_DOWNLOAD_EXPIRED" if kind == "file" else "UNAVAILABLE") in wire
            await frames.put(encode(raw_event(2, time=int(datetime.now(UTC).timestamp()),
                                              text="下一条消息仍然可以处理")))
            await until(lambda: len(core.store.rows("SELECT * FROM events")) == 2, tasks=tasks)
            await sdk_reply(core, tmp_path, 2, "下一条也收到了。")
            assert all(not task.done() for task in tasks)
            assert runtime.row()["state"] == "ACTIVE" and len(downloads) == 1
            assert core.store.rows("SELECT state FROM model_usage") == [
                {"state": "SETTLED"}, {"state": "SETTLED"}]
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert not [item for item in results if isinstance(item, Exception)]

    async def scenario():
        async with serve(source, "127.0.0.1", 0) as server:
            ws_url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            if kind == "voice":
                async with voice_fixture(core, tmp_path, ws_url=ws_url) as (
                    runtime, client, protocol, _, _,
                ):
                    await exercise(runtime, client, protocol)
            else:
                configure(core)
                async with window(core, tmp_path, media=True, content=True) as (_, cfg, plan):
                    spool, runtime = native_spool(core, tmp_path,
                        replace(cfg, onebot_ws_url=ws_url), plan)
                    try:
                        async with private_http_client(cfg.core_url, cfg.bridge_token) as client, \
                            private_http_client(cfg.onebot_http_url, cfg.onebot_token,
                            transport=httpx.MockTransport(native)) as protocol:
                            await exercise(runtime, client, protocol)
                    finally:
                        spool.close()
        assert VOICE_URL not in encode(lookups)
    run(scenario())
