"""Native signed files reuse real HTTP intake and the existing isolated worker."""
import base64
import hashlib
import json

import httpx
import pytest

from companion import content_files, file_parser, owner_authority
from companion.owner_authority_protocol import AuthorityStop
from companion.store import encode
from companion_delivery_protocol.authority import file_upload_signing_payload
from tests.conftest import run
from tests.support.native_file_pipeline import configure
from tests.support.native_image_pipeline import admit_image
from tests.support.prepare_owner_input import window


async def upload(core, client, config, plan, tmp_path):
    data = b"first\n\nsecond"
    event, _ = await admit_image(core, client, config, plan, tmp_path, message=[
        {"type": "file", "data": {"file_id": "private-file", "file": "notes.txt",
                                  "file_size": len(data)}}])
    original = event["payload"]["rich"]["segments"][0]
    path = "/internal/v2/content/" + event["event_id"]
    response = await client.post(path + "/file/claim", json={"request_key": "file:0",
        "source_sha256": original["source_sha256"]})
    assert response.status_code == 200, response.text
    lease = response.json()
    proof = {"authority": plan.authority, "event_id": event["event_id"], "slot": 0,
             "source_sha256": original["source_sha256"], "lease_token": lease["lease_token"],
             "file_id": original["file_id"], "filename": original["filename"],
             "media_sha256": hashlib.sha256(data).hexdigest(), "mime": "text/plain",
             "bytes": len(data)}
    proof["signature"] = plan.key.sign(file_upload_signing_payload(proof)).hex()
    headers = {"Content-Type": "text/plain", "X-Content-Key": "file:0",
               "X-Content-Lease": lease["lease_token"],
               "X-Content-Proof": base64.urlsafe_b64encode(encode(proof).encode()).decode()}
    response = await client.post(path + "/file", content=data, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "QUEUED"
    assert (await client.post(path + "/file", content=data, headers=headers)).json()["duplicate"]
    return event, data


def test_native_file_http_upload_and_guarded_worker(core, tmp_path):
    async def scenario():
        configure(core)
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, data = await upload(core, client, config, plan, tmp_path)
            calls = []

            def parser(blob, filename, mime, **kwargs):
                kwargs["effect_check"]()
                calls.append(blob)
                return file_parser.parse_document(blob, filename, mime,
                    limits=kwargs["limits"]) | {"isolation": "docker_network_none"}

            assert await content_files.run_file_once(core.content, parse=parser)
            assert calls == [data]
            row = core.store.one('SELECT * FROM content_items WHERE owner_event_id=%s',
                                 (event["event_id"],))
            assert row["state"] == "SUCCEEDED", dict(row)
            observed = core.store.one("SELECT * FROM observations")
            assert json.loads(observed["evidence_json"])["document"]["filename"] == "notes.txt"
    run(scenario())


def test_stop_during_parser_setup_blocks_byte_dispatch(core, tmp_path):
    async def scenario():
        configure(core)
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            await upload(core, client, config, plan, tmp_path)
            calls = []

            def parser(blob, filename, mime, **kwargs):
                owner_authority.stop(core.store, core.settings, core.clock,
                    AuthorityStop(operation_id="stop-file-dispatch", authority=plan.authority))
                kwargs["effect_check"]()
                calls.append(blob)
                return file_parser.parse_document(blob, filename, mime)

            assert await content_files.run_file_once(core.content, parse=parser)
            assert calls == []
            assert not core.store.one("SELECT 1 FROM observations")
            assert core.store.one("SELECT state FROM content_items")["state"] == "FAILED"
    run(scenario())


@pytest.mark.parametrize("recovery", ["pending", "downloading", "lost_receipt"])
def test_native_file_bridge_queue_recovery(core, tmp_path, recovery):
    from companion_onebot_bridge import pending_media
    from companion_onebot_bridge.capture import prepare_owner_input
    from companion_onebot_bridge.network import private_http_client
    from companion_onebot_bridge.onebot_client import OneBotClient
    from tests.support.native_media_queue import row
    from tests.support.onebot_events import raw_event
    from tests.support.prepare_owner_input import native_spool

    async def scenario():
        configure(core)
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            spool, runtime = native_spool(core, tmp_path, config, plan)
            body = b"native private file"
            claims, uploads, lookups, downloads = [], [], [], []
            try:
                frame = encode(raw_event(time=int(core.clock.now.timestamp()), message=[
                    {"type": "file", "data": {"file_id": "native-file", "file": "notes.txt",
                                              "file_size": len(body)}}]))
                prepared = await prepare_owner_input(runtime, client, None, frame, core.clock.now,
                                                      ("127.0.0.1", 3001))
                event_id = prepared["event"]["event_id"]
                runtime.persist_capture(prepared["event"], frame, received_at=core.clock.now,
                                        peer=("127.0.0.1", 3001), media=prepared["media"])
                assert await runtime.flush_one(client)
                if recovery == "downloading":
                    pending_media.transition(runtime, event_id, "CLAIMING", 0)
                    response = await client.post("/internal/v2/content/" + event_id + "/file/claim",
                        json={"request_key": "file:0",
                              "source_sha256": prepared["media"]["files"][0]["source_sha256"]})
                    assert response.status_code == 200, response.text
                    original_claim = response.json()
                    pending_media.transition(runtime, event_id, "DOWNLOADING", 0, original_claim)
                    core.clock.advance(core.settings.private_files.upload_lease_seconds + 1)
                    assert await content_files.run_file_once(core.content)
            finally:
                spool.close()

            async def lookup(request):
                assert request.url.path == "/get_private_file_url"
                assert json.loads(request.content)["file_id"] == "native-file"
                lookups.append(request)
                return httpx.Response(200, json={"status": "ok", "retcode": 0,
                    "data": {"url": "https://files.example.test/native"}})

            async def download(request):
                downloads.append(request)
                return httpx.Response(200, content=body, headers={"Content-Type": "text/plain"})

            async def resolve(host, port):
                return ["8.8.8.8"]

            async def request_hook(request):
                if request.url.path.endswith("/file/claim"):
                    claims.append(request)
                if request.url.path.endswith("/file"):
                    uploads.append(request)

            async def response_hook(response):
                if response.request.url.path.endswith("/file") and recovery == "lost_receipt":
                    raise httpx.ReadTimeout("synthetic lost response", request=response.request)

            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                async with private_http_client(config.core_url, config.bridge_token) as transfer:
                    async with private_http_client(config.onebot_http_url, config.onebot_token,
                        transport=httpx.MockTransport(lookup)) as native:
                        transfer.event_hooks["request"].append(request_hook)
                        transfer.event_hooks["response"].append(response_hook)
                        pump = pending_media.MediaPump(runtime, transfer,
                            protocol=OneBotClient(config, native),
                            file_transport=httpx.MockTransport(download), file_resolver=resolve)
                        if recovery == "lost_receipt":
                            with pytest.raises(httpx.ReadTimeout):
                                await pump.execute(row(runtime))
                            assert row(runtime)["state"] == "UPLOADING"
                        else:
                            await pump.execute(row(runtime))
                            assert row(runtime)["state"] == "DONE"
            finally:
                spool.close()
            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                if recovery == "lost_receipt":
                    async with private_http_client(config.core_url,
                                                    config.bridge_token) as transfer:
                        transfer.event_hooks["request"].append(request_hook)
                        await pending_media.MediaPump(runtime, transfer).execute(row(runtime))
                assert row(runtime)["state"] == "DONE"
                runtime.verify()
                assert len(claims) == (0 if recovery == "downloading" else 1)
                assert len(uploads) == len(lookups) == len(downloads) == (
                    0 if recovery == "downloading" else 1)
                item = core.store.one("SELECT * FROM content_items WHERE kind='qq_file'")
                if recovery == "downloading":
                    assert item["lease_token"] == original_claim["lease_token"]
                    assert item["state"] == "FAILED"
                    assert item["error_code"] == "PRIVATE_FILE_DOWNLOAD_EXPIRED"
                else:
                    assert item["state"] == "QUEUED"
            finally:
                spool.close()
    run(scenario())


def test_native_file_reaches_sdk_in_independent_worker(core, tmp_path):
    import asyncio
    import pickle
    import sys
    from pathlib import Path

    async def scenario():
        configure(core)
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, data = await upload(core, client, config, plan, tmp_path)
            job = core.store.one('SELECT * FROM jobs WHERE event_id=%s', (event["event_id"],))
            arguments, output = tmp_path / "file-worker.pickle", tmp_path / "file-wire.json"
            arguments.write_bytes(pickle.dumps((core.settings, core.clock.now, job["job_id"],
                                                data, str(output))))
            process = await asyncio.create_subprocess_exec(sys.executable,
                str(Path(__file__).with_name("file_sdk_worker.py")), str(arguments),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                logs, _ = await asyncio.wait_for(process.communicate(), 45)
            except BaseException:
                process.kill()
                await process.wait()
                raise
            assert process.returncode == 0, logs.decode(errors="replace")
            requests = json.loads(output.read_text(encoding="utf-8"))
            assert len(requests) == 1
            wire = encode(requests[0]["messages"])
            assert "first" in wire and "second" in wire
            assert "notes.txt" in wire
            assert "external_untrusted" in wire
            assert "主人发送了一条非文本消息。" in wire
            assert "请看看我分享的内容。" not in wire
            assert core.store.rows("SELECT scenario,state FROM model_usage") == [
                {"scenario": "chat", "state": "SETTLED"}]
    run(scenario())


def test_native_file_original_survives_new_turn_until_its_own_deadline(core, tmp_path):
    from companion import image_spool
    from companion.core import Core

    async def scenario():
        configure(core)
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, data = await upload(core, client, config, plan, tmp_path)
            await admit_image(core, client, config, plan, tmp_path, number=43, message=[
                {"type": "text", "data": {"text": "补充说明"}}])
            job = core.store.one('SELECT * FROM jobs WHERE event_id=%s', (event["event_id"],))
            assert job["state"] == "CANCELLED"
            with Core(core.settings, clock=core.clock) as restarted:
                row = restarted.store.one("SELECT * FROM content_items WHERE kind='qq_file'")
                assert image_spool.get(restarted.store, row, core.clock(), 1000)[0] == data
                core.clock.advance(76)
                image_spool.cleanup(restarted.store, core.clock())
                path = image_spool.directory(restarted.store) / (row["item_id"] + ".raw")
                assert not path.exists()
    run(scenario())


@pytest.mark.parametrize("boundary", ["dns_stop", "upload_proof"])
def test_native_file_guards_final_download_and_upload(core, tmp_path, boundary):
    from companion_onebot_bridge.files import OriginalFile, upload_original
    from companion_onebot_bridge.network import private_http_client
    from companion_onebot_bridge.onebot_client import OneBotClient
    from tests.support.prepare_owner_input import native_spool

    async def scenario():
        configure(core)
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            data = b"private file"
            event, _ = await admit_image(core, client, config, plan, tmp_path, message=[
                {"type": "file", "data": {"file_id": "native-file", "file": "notes.txt",
                                          "file_size": len(data)}}])
            segment = event["payload"]["rich"]["segments"][0]
            original = OriginalFile(**{name: segment[name] for name in (
                "index", "slot", "source_sha256", "file_id", "filename", "size")})
            downloads, lookups = [], []

            async def lookup(request):
                lookups.append(request)
                return httpx.Response(200, json={"status": "ok", "retcode": 0,
                    "data": {"url": "https://files.example.test/native"}})

            async def resolve(host, port):
                if boundary == "dns_stop":
                    owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                        operation_id="stop-file-dns", authority=plan.authority))
                return ["8.8.8.8"]

            async def download(request):
                downloads.append(request)
                return httpx.Response(200, content=data, headers={"Content-Type": "text/plain"})

            async def mutate(request):
                if request.url.path.endswith("/file"):
                    request.headers["X-Content-Proof"] = "e30="

            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                async with private_http_client(config.core_url, config.bridge_token) as transfer:
                    if boundary == "upload_proof":
                        transfer.event_hooks["request"].append(mutate)
                    async with private_http_client(config.onebot_http_url, config.onebot_token,
                        transport=httpx.MockTransport(lookup)) as native:
                        with pytest.raises((httpx.HTTPStatusError, ValueError)):
                            await upload_original(runtime, transfer, event["event_id"], original,
                                onebot=OneBotClient(config, native),
                                transport=httpx.MockTransport(download), resolver=resolve)
                assert len(lookups) == 1
                assert len(downloads) == (0 if boundary == "dns_stop" else 1)
                assert core.store.one("SELECT state FROM content_items")["state"] == "FETCHING"
                assert not core.store.one('SELECT 1 FROM runtime_meta WHERE key LIKE %s',
                                           ("live_text_file_upload:%",))
                assert not core.store.one("SELECT 1 FROM model_usage")
            finally:
                spool.close()
    run(scenario())


@pytest.mark.parametrize("retire", ["stop", "recall", "expiry"])
def test_native_file_retirement_never_dispatches_parser(core, tmp_path, retire):
    from companion import image_spool, qq_files
    from companion.owner_authority_protocol import OwnerCapture
    from tests.support.owner_recall import receipt

    async def scenario():
        configure(core)
        async with window(core, tmp_path, media=True, content=True) as (client, config, plan):
            event, _ = await upload(core, client, config, plan, tmp_path)
            row = core.store.one("SELECT * FROM content_items WHERE kind='qq_file'")
            proof = json.loads(core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                (qq_files.STAGED + row["item_id"],))["value"])
            if retire == "stop":
                owner_authority.stop(core.store, core.settings, core.clock, AuthorityStop(
                    operation_id="stop-file-worker", authority=plan.authority))
                historical = owner_authority.evidence(core.store, core.settings, plan.authority)
                original = qq_files.manifest(event["payload"])[0]
                assert qq_files.upload_proof(historical, event["event_id"], original,
                    proof["lease_token"], proof["media_sha256"], proof["mime"], proof["bytes"],
                    proof) == proof
            elif retire == "recall":
                saved = json.loads(core.store.one('SELECT value FROM runtime_meta WHERE key=%s',
                    ("owner_input:" + event["event_id"],))["value"])
                notice = receipt(core, OwnerCapture.model_validate(saved["capture"]), plan.key)
                withdrawn = await client.post("/internal/v1/owner-authority/inputs/recall",
                    json=notice.model_dump(mode="json"))
                assert withdrawn.status_code == 200, withdrawn.text
            else:
                core.clock.advance(core.settings.private_files.wait_seconds + 1)
            calls = []

            def parser(*args, **kwargs):
                calls.append(True)
                pytest.fail("retired file reached parser")

            await content_files.run_file_once(core.content, parse=parser)
            assert calls == []
            assert not core.store.one("SELECT 1 FROM observations")
            assert not (image_spool.directory(core.store) / (row["item_id"] + ".raw")).exists()
            retry = await client.post("/internal/v2/content/" + event["event_id"] + "/file/claim",
                json={"request_key": "file:0", "source_sha256": proof["source_sha256"]})
            assert retry.status_code in {403, 409, 410}
    run(scenario())
