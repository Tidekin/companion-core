"""The original media queue recovers native transfers without replaying uploads."""

import asyncio
import json
import pickle
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from companion.store import encode
from companion_onebot_bridge import pending_media
from companion_onebot_bridge.network import private_http_client
from tests.conftest import run
from tests.support.native_image_pipeline import pixels
from tests.support.native_media_queue import captured, row
from tests.support.onebot_events import raw_event
from tests.support.prepare_owner_input import native_spool, window


@pytest.mark.parametrize("kind", ["text_image", "images_then_text", "card_image"])
def test_native_image_queue_preserves_lease_under_core_roundtrip_latency(core, tmp_path, kind):
    from tests.sdk_reader_fixture import answer

    async def scenario():
        async with window(core, tmp_path, media=True, content=True, history=True,
                          image_count="unlimited") as (client, config, plan):
            spool, runtime = native_spool(core, tmp_path, config, plan)
            downloads, requests = [], []

            async def resolve(host, port):
                return ["8.8.8.8"]

            async def download(request):
                downloads.append(request)
                return httpx.Response(200, content=pixels(), headers={"content-type": "image/png"})

            async def latency(response):
                requests.append(response.request.url.path)
                # The synthetic Core and Bridge clocks share elapsed roundtrip time.
                core.clock.advance(0.5)

            try:
                image = {"type": "image", "data": {"url": "https://gchat.qpic.cn/latency-original"}}
                message = [image, {"type": "text", "data": {"text": "请描述这张图片。"}}]
                if kind == "card_image":
                    message.append({"type": "json", "data": {"data": json.dumps({
                        "meta": {"news": {"jumpUrl": "https://www.bilibili.com/video/BV1xx411c7mD"}},
                    })}})
                prepared = await captured(core, client, runtime, message=message)
                if kind == "images_then_text":
                    await captured(core, client, runtime, number=43, message=[image])
                    prepared = await captured(core, client, runtime, number=44, message=[
                        {"type": "text", "data": {"text": "请结合刚才的两张原图。"}}])
                async with private_http_client(config.core_url, config.bridge_token) as transfer:
                    transfer.event_hooks["response"].append(latency)
                    pump = pending_media.MediaPump(runtime, transfer,
                        image_transport=httpx.MockTransport(download), image_resolver=resolve)
                    while current := pending_media._execute(runtime,
                        "SELECT * FROM automatic_media WHERE state<>'DONE' ORDER BY rowid"
                    ).fetchone():
                        await pump.execute(current)
                count = 2 if kind == "images_then_text" else 1
                assert len(downloads) == count, requests
                assert core.store.rows("SELECT state FROM content_items WHERE kind='qq_image'") == (
                    [{"state": "SUCCEEDED"}] * count)
                assert len(core.store.rows("SELECT * FROM captured_images")) == count
                assert not core.store.rows("SELECT * FROM model_usage")
                runtime.verify()
            finally:
                spool.close()
            job = core.store.one("SELECT * FROM jobs WHERE event_id=?",
                                 (prepared["event"]["event_id"],))
            config_path, output = tmp_path / "latency-worker.pickle", tmp_path / "latency-wire.json"
            config_path.write_bytes(pickle.dumps((core.settings, core.clock.now, job["job_id"],
                [answer("已经收到原图。")], "SUCCEEDED", str(output))))
            process = await asyncio.create_subprocess_exec(sys.executable,
                str(Path(__file__).with_name("reader_sdk_worker.py")), str(config_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                logs, _ = await asyncio.wait_for(process.communicate(), 45)
            except BaseException:
                process.kill()
                await process.wait()
                raise
            assert process.returncode == 0, logs.decode(errors="replace")
            wire = json.loads(output.read_text(encoding="utf-8"))
            assert len(wire) == 1
            image_parts = [part for message in wire[0]["messages"]
                           if isinstance(message["content"], list) for part in message["content"]
                           if part["type"] == "image_url"]
            assert len(image_parts) == count
            if kind == "images_then_text":
                assert wire[0]["messages"][-1]["content"] == "请结合刚才的两张原图。"
            assert core.store.rows("SELECT origin_event_id FROM outbox") == [
                {"origin_event_id": prepared["event"]["event_id"]}]
    run(scenario())


@pytest.mark.parametrize("recovery", [
    "pending", "uncommitted_claim", "downloading", "lost_receipt",
])
def test_original_queue_recovers_native_transfer(core, tmp_path, recovery):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                prepared = await captured(core, client, runtime)
                if recovery == "uncommitted_claim":
                    pending_media.transition(runtime, prepared["event"]["event_id"], "CLAIMING", 0)
                if recovery == "downloading":
                    image = prepared["media"]["images"][0]
                    pending_media.transition(runtime, prepared["event"]["event_id"], "CLAIMING", 0)
                    response = await client.post("/internal/v2/content/" +
                        prepared["event"]["event_id"] + "/image/claim", json={
                            "request_key": "image:0", "source_sha256": image["source_sha256"]})
                    assert response.status_code == 200
                    pending_media.transition(runtime, prepared["event"]["event_id"],
                                               "DOWNLOADING", 0, response.json())
                    original_claim = response.json()
                    core.clock.advance(61)
                    await core.content.run_once()
            finally:
                spool.close()
            downloads, claims, uploads = [], [], []

            async def resolve(host, port):
                return ["8.8.8.8"]

            async def download(request):
                downloads.append(request)
                return httpx.Response(200, content=pixels(), headers={"content-type": "image/png"})

            async def request_hook(request):
                if request.url.path.endswith("/image/claim"):
                    claims.append(request.url.path)
                if request.url.path.endswith("/image"):
                    uploads.append(request.url.path)

            async def response_hook(response):
                if response.request.url.path.endswith("/image") and recovery == "lost_receipt":
                    raise httpx.ReadTimeout("synthetic lost receipt", request=response.request)

            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                async with private_http_client(config.core_url, config.bridge_token) as transfer:
                    transfer.event_hooks["request"].append(request_hook)
                    transfer.event_hooks["response"].append(response_hook)
                    pump = pending_media.MediaPump(runtime, transfer,
                        image_transport=httpx.MockTransport(download), image_resolver=resolve)
                    if recovery == "uncommitted_claim":
                        await pump.execute(row(runtime))
                        assert row(runtime)["state"] == "PENDING" and not claims
                        assert core.store.one("SELECT state FROM content_items")["state"] == (
                            "WAITING")
                    if recovery == "lost_receipt":
                        with pytest.raises(httpx.ReadTimeout):
                            await pump.execute(row(runtime))
                        assert row(runtime)["state"] == "UPLOADING"
                    else:
                        await pump.execute(row(runtime))
                        assert row(runtime)["state"] == "DONE"
                runtime.verify()
            finally:
                spool.close()
            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                async with private_http_client(config.core_url, config.bridge_token) as transfer:
                    transfer.event_hooks["request"].append(request_hook)
                    pump = pending_media.MediaPump(runtime, transfer,
                        image_transport=httpx.MockTransport(download), image_resolver=resolve)
                    if recovery == "lost_receipt":
                        await pump.execute(row(runtime))
                    assert row(runtime)["state"] == "DONE"
                    assert row(runtime)["manifest_json"] is None
                    runtime.verify()
                assert len(downloads) == len(uploads) == (0 if recovery == "downloading" else 1)
                assert len(claims) == (0 if recovery == "downloading" else 1)
                item = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
                if recovery == "downloading":
                    assert item["state"] == "FAILED"
                    assert item["lease_token"] == original_claim["lease_token"]
                    assert item["lease_until"] == original_claim["lease_until"]
                assert len(core.store.rows("SELECT * FROM captured_images")) == (
                    0 if recovery == "downloading" else 1)
                assert not core.store.rows("SELECT * FROM model_usage")
            finally:
                spool.close()
    run(scenario())


def test_native_queue_manifest_tamper_blocks_verification(core, tmp_path):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                await captured(core, client, runtime)
                value = json.loads(row(runtime)["manifest_json"])
                value["images"][0]["url"] = "https://gchat.qpic.cn/replaced"
                with runtime.db:
                    runtime.db.execute("UPDATE automatic_media SET manifest_json=?",
                                       (encode(value),))
                with pytest.raises(ValueError, match="AUTOMATIC_MEDIA_JOURNAL_CHANGED"):
                    runtime.verify()
            finally:
                spool.close()
    run(scenario())


def test_uncertain_native_claim_is_not_reissued(core, tmp_path):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                prepared = await captured(core, client, runtime)
                event_id = prepared["event"]["event_id"]
                pending_media.transition(runtime, event_id, "CLAIMING", 0)
                response = await client.post("/internal/v2/content/" + event_id + "/image/claim",
                    json={"request_key": "image:0",
                          "source_sha256": prepared["media"]["images"][0]["source_sha256"]})
                assert response.status_code == 200
                original = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
                core.clock.advance(61)
                await core.content.run_once()
            finally:
                spool.close()
            spool, runtime = native_spool(core, tmp_path, config, plan)
            requests = []

            async def request_hook(request):
                requests.append(request.url.path)

            try:
                async with private_http_client(config.core_url, config.bridge_token) as transfer:
                    transfer.event_hooks["request"].append(request_hook)
                    pump = pending_media.MediaPump(runtime, transfer)
                    await pump.execute(row(runtime))
                assert row(runtime)["state"] == "DONE"
                assert not any(path.endswith(("/claim", "/image")) for path in requests)
                closed = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
                assert closed["state"] == "FAILED"
                assert {key: closed[key] for key in ("lease_token", "lease_until", "reserved_bytes")
                        } == {key: original[key]
                              for key in ("lease_token", "lease_until", "reserved_bytes")}
                assert not core.store.rows("SELECT * FROM captured_images")
                runtime.verify()
            finally:
                spool.close()
    run(scenario())


@pytest.mark.parametrize("change", ["slot", "kind", "duplicate"])
def test_failed_status_must_identify_the_original_media_slot(core, tmp_path, change):
    async def scenario():
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                prepared = await captured(core, client, runtime)
                event_id = prepared["event"]["event_id"]
                path = "/internal/v2/content/" + event_id
                pending_media.transition(runtime, event_id, "CLAIMING", 0)
                response = await client.post(path + "/image/claim", json={
                    "request_key": "image:0",
                    "source_sha256": prepared["media"]["images"][0]["source_sha256"]})
                assert response.status_code == 200
                pending_media.transition(runtime, event_id, "DOWNLOADING", 0, response.json())
                core.clock.advance(61)
                await core.content.run_once()
                before = dict(row(runtime))

                async def changed(response):
                    if response.request.url.path != path:
                        return
                    await response.aread()
                    body = response.json()
                    if change == "duplicate":
                        body["items"].append(body["items"][0])
                    else:
                        body["items"][0][change] = 1 if change == "slot" else "qq_file"
                    response._content = encode(body).encode()

                async with private_http_client(config.core_url, config.bridge_token) as transfer:
                    transfer.event_hooks["response"].append(changed)
                    pump = pending_media.MediaPump(runtime, transfer,
                        image_transport=httpx.MockTransport(
                            lambda _: pytest.fail("No repeated GET")))
                    with pytest.raises(ValueError, match="INVALID_CORE_RECEIPT") as caught:
                        await pump.execute(row(runtime))
                assert caught.value.capture_diagnostic.phase == "core_check"
                assert dict(row(runtime)) == before
                assert not core.store.rows("SELECT * FROM captured_images")
                runtime.verify()
            finally:
                spool.close()
    run(scenario())


@pytest.mark.parametrize("kind", [
    "image", "file", "animation", "reply_image", "forward_image", "reply_file", "forward_file",
])
def test_websocket_daemon_queue_reaches_independent_sdk_worker(core, tmp_path, kind):
    from websockets.asyncio.server import serve

    from companion_onebot_bridge.app import receive, transfer_media, upload
    from companion_onebot_bridge.metrics import Metrics
    from companion_onebot_bridge.onebot_client import OneBotClient
    from tests.sdk_reader_fixture import answer
    from tests.support.native_file_pipeline import configure

    async def scenario():
        if kind.endswith("file"):
            configure(core)
        async with window(core, tmp_path, media=True, content=True,
                          video=kind == "animation") as (client, config, plan):
            stop, wake, uploaded = asyncio.Event(), asyncio.Event(), asyncio.Event()
            media_kind = "file" if kind.endswith("file") else "image"
            mime = "image/gif" if kind == "animation" else "image/png"
            data = pixels(mime) if media_kind == "image" else b"first\n\nsecond"
            segment = ({"type": "image", "data": {"url": "https://gchat.qpic.cn/daemon-original"}}
                if media_kind == "image" else {"type": "file", "data": {"file_id": "daemon-file",
                    "file": "notes.txt", "file_size": len(data)}})
            reference = kind.split("_", 1)[0] if "_" in kind else None
            inbound = ({"type": reference, "data": {
                "id": "-42" if reference == "reply" else "original-forward"}}
                if reference else segment)
            frame = encode(raw_event(time=int(core.clock.now.timestamp()), message=[inbound]))
            lookups, downloads = [], []

            async def server(socket):
                assert socket.request.headers["Authorization"] == "Bearer " + config.onebot_token
                core.clock.now = datetime.now(UTC)
                await socket.send(frame)
                await stop.wait()

            async def resolve(host, port):
                return ["8.8.8.8"]

            async def downloaded(request):
                downloads.append(str(request.url))
                return httpx.Response(200, content=data, headers={
                    "content-type": mime if media_kind == "image" else "text/plain"})

            async def lookup(request):
                lookups.append(request.url.path)
                if request.url.path in {"/get_msg", "/get_forward_msg"}:
                    value = ({"message_id": -42, "message": [segment]} if reference == "reply"
                             else {"messages": [{"type": "node", "data": {
                                 "name": "引用作者", "content": [segment]}}]})
                    return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                                    "data": value})
                assert media_kind == "file" and request.url.path == "/get_private_file_url"
                return httpx.Response(200, json={"status": "ok", "retcode": 0,
                    "data": {"url": "https://files.example.test/daemon-file"}})

            async def response_hook(response):
                if response.request.url.path.endswith("/" + media_kind):
                    assert response.status_code == 200
                    uploaded.set()

            async with serve(server, "127.0.0.1", 0) as listener:
                config = replace(config, onebot_ws_url=
                                 f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}")
                spool, runtime = native_spool(core, tmp_path, config, plan)
                try:
                    async with private_http_client(
                        config.core_url, config.bridge_token,
                    ) as transfer, private_http_client(config.onebot_http_url,
                        config.onebot_token, transport=httpx.MockTransport(lookup)) as native:
                        transfer.event_hooks["response"].append(response_hook)
                        metrics = Metrics()
                        protocol = OneBotClient(config, native)
                        pump = pending_media.MediaPump(runtime, transfer, protocol=protocol,
                            image_transport=httpx.MockTransport(downloaded), image_resolver=resolve,
                            file_transport=httpx.MockTransport(downloaded), file_resolver=resolve)
                        tasks = [asyncio.create_task(receive(runtime.config, spool, wake, stop,
                            metrics, protocol, runtime=runtime, core=transfer)),
                            asyncio.create_task(upload(transfer, spool, wake, stop, metrics,
                                                       runtime=runtime)),
                            asyncio.create_task(transfer_media(pump, stop))]
                        completion = asyncio.create_task(uploaded.wait())
                        try:
                            done, _ = await asyncio.wait([completion, *tasks], timeout=20,
                                                       return_when=asyncio.FIRST_COMPLETED)
                            for task in done:
                                task.result()
                            assert completion in done, "daemon did not finish the media upload"
                            await asyncio.wait_for(asyncio.shield(pump.task), 5)
                            assert row(runtime)["state"] == "DONE"
                            runtime.verify()
                        finally:
                            stop.set()
                            completion.cancel()
                            for task in tasks:
                                task.cancel()
                            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
                        assert all(isinstance(result, asyncio.CancelledError) or result is None
                                   for result in outcomes), outcomes
                finally:
                    spool.close()
            job = core.store.one("SELECT * FROM jobs WHERE kind='chat'")
            assert len(downloads) == 1
            if reference:
                action = "/get_msg" if reference == "reply" else "/get_forward_msg"
                assert lookups.count(action) == 1
                body = json.loads(core.store.one("SELECT body_json FROM events WHERE event_id=?",
                                                 (job["event_id"],))["body_json"])
                nested = body["rich"]["segments"][0]["body"]["media"][0]
                assert nested["origin"]["kind"] == reference
                assert nested["path"] == ([0, 0] if reference == "reply" else [0, 0, 0])
            config_path, output = tmp_path / "queue-worker.pickle", tmp_path / "queue-wire.json"
            arguments = ((core.settings, core.clock.now, job["job_id"],
                [answer("原图已经收到。")], "SUCCEEDED", str(output)) if media_kind == "image" else
                (core.settings, core.clock.now, job["job_id"], data, str(output)))
            if kind == "animation":
                from tests.support.native_video import responses

                arguments = (core.settings, core.clock.now, job["job_id"],
                             responses(), "SUCCEEDED", str(output))
            config_path.write_bytes(pickle.dumps(arguments))
            worker_script = ("reader_sdk_worker.py" if media_kind == "image" else
                             "file_sdk_worker.py")
            process = await asyncio.create_subprocess_exec(sys.executable,
                str(Path(__file__).with_name(worker_script)), str(config_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                logs, _ = await asyncio.wait_for(process.communicate(), 45)
            except BaseException:
                process.kill()
                await process.wait()
                raise
            assert process.returncode == 0, logs.decode(errors="replace")
            requests = json.loads(output.read_text(encoding="utf-8"))
            assert len(requests) == (3 if kind == "animation" else 1)
            if media_kind == "image":
                assert any(part["type"] == "image_url" for message in requests[0]["messages"]
                           if isinstance(message["content"], list) for part in message["content"])
                if kind == "animation":
                    assert "video_url" in encode(requests[1]["messages"])
                    assert "one_animation_cycle" in encode(requests[2]["messages"])
            else:
                wire = encode(requests[0]["messages"])
                assert "first" in wire and "second" in wire and "notes.txt" in wire
                assert "external_untrusted" in wire
            assert len(core.store.rows("SELECT * FROM outbox")) == 1
    run(scenario())
