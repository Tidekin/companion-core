"""Real Core TLS and independent node/child, then native checked OneBot delivery."""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion.voice_jobs import VoiceJobs
from companion.voice_protocol import digest
from companion_onebot_bridge.onebot_client import OneBotClient
from companion_onebot_bridge.voice_media import prepare_delivery
from companion_voice_node.worker import VoiceNodeConfig, local_manifest
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, worker_for
from tests.support.applio_render import resources
from tests.support.singing_integration import queue_song, select_song, singing_window
from tests.support.voice_jobs import TOKEN
from tests.support.voice_node_api import tls_core
from tests.support.voice_node_worker import node as node  # noqa: F401 -- pytest fixture


async def independent(config, cert, counter, tmp_path, timestamp):
    path = tmp_path / "singing-node.private.json"
    path.write_text(json.dumps(config.model_dump(mode="json") | {
        "credential": config.credential.get_secret_value()}))
    process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
        "tests.support.singing_node_process", str(path), str(cert), str(counter), str(timestamp),
        cwd=str(config.workspace), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        assert process.returncode == 0, stderr.decode(errors="replace")
        receipt = json.loads(stdout)
        assert receipt["pid"] != os.getpid()
        assert receipt["outcome"] == "COMPLETED" and receipt["requests"] == 4
        assert receipt["control_requests"] < 40
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        path.unlink(missing_ok=True)


@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("short_link", [False, True])
def test_native_singing_process_and_ordered_delivery(
    core, tmp_path, monkeypatch, node, uncertain, short_link,
):
    config, _, counter = node
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("61" * 32))
    config = config.model_copy(update={"node_id": "local", "credential": TOKEN,
        "core_public_key": key.public_key().public_bytes_raw().hex(),
        "singing": resources(tmp_path / "singing"), "request_timeout_seconds": 5,
        "execution_timeout_seconds": 60, "startup_timeout_seconds": 30})
    from pydantic import SecretStr

    config = config.model_copy(update={"credential": SecretStr(TOKEN)})
    config = VoiceNodeConfig.model_validate(config.model_dump() | {"credential": TOKEN})
    manifest = digest(local_manifest(config))

    async def scenario():
        async with singing_window(core, tmp_path, monkeypatch,
            manifest_sha256=manifest) as (client, bridge, plan):
            _, admitted, job = await queue_song(core, client, bridge, plan, short_link=short_link)
            async with tls_core(core, tmp_path) as (origin, cert):
                await independent(config.model_copy(update={"core_origin": origin}), cert, counter,
                                  tmp_path, core.clock.now.timestamp())
            receipt = json.loads(counter.read_text())
            assert receipt["pid"] != os.getpid()
            assert not {"credential", "core_origin", "authorization"} & set(receipt["packet_keys"])
            assert not any("TOKEN" in key or "KEY" in key for key in receipt["environment"])
            assert VoiceJobs(core.store, core.settings, core.clock).status(job["job_id"])[
                "state"] == "READY"
            rows = await select_song(core, admitted, job["job_id"])
            sent = []

            def native(request):
                assert request.url.path == "/send_private_msg"
                sent.append(json.loads(request.content))
                return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                                  "data": {"message_id": 100 + len(sent)}})

            async with httpx.AsyncClient(base_url=bridge.onebot_http_url,
                transport=httpx.MockTransport(native)) as native_http:
                onebot = OneBotClient(replace(bridge, delivery_enabled=True), native_http)
                for row in rows:
                    response = await client.post("/internal/v1/deliveries/lease", json={
                        "worker_id": "native-song-bridge", "authority": plan.authority})
                    assert response.status_code == 200, response.text
                    item = response.json()["items"][0]
                    assert item["message_id"] == row["message_id"]
                    prepared = prepare_delivery(item, bridge, authority=plan.authority,
                        voice_policy_sha256=core.settings.voice.authority_sha256(),
                        now=datetime.fromisoformat(core.clock()))
                    path = "/internal/v1/deliveries/" + item["message_id"]
                    begun = await client.post(path + "/begin-send", json={
                        "lease_token": item["lease_token"]})
                    assert begun.status_code == 200, begun.text
                    checked = await client.post("/internal/v1/owner-authority/send-check", json={
                        "authority": plan.authority, "message_id": item["message_id"],
                        "event_id": job["event_id"], "lease_token": item["lease_token"],
                        "content_sha256": digest(item["content"]),
                        "reply_revision": item["reply_revision"], "voice": prepared.check})
                    assert checked.status_code == 200, checked.text
                    provider_id = await onebot.send_message(prepared.message)
                    ack = {"lease_token": item["lease_token"],
                           "result": "DELIVERY_UNCERTAIN" if uncertain else "SENT"}
                    if not uncertain:
                        ack["provider_message_id"] = provider_id
                    response = await client.post(path + "/result", json=ack)
                    assert response.status_code == 200, response.text
                    if uncertain:
                        break
            if uncertain:
                assert len(sent) == 1
                assert core.store.rows("SELECT state FROM outbox ORDER BY segment_index") == [
                    {"state": "DELIVERY_UNCERTAIN"}, {"state": "CANCELLED"}]
                # Reopen Core and its recovery path; never create another provider call or song.
                async with worker_for(core, Provider()) as worker:
                    assert await worker.run_once("restart-after-unknown") is False
                response = await client.post("/internal/v1/deliveries/lease", json={
                    "worker_id": "after-unknown", "authority": plan.authority})
                assert response.status_code == 200 and not response.json()["items"]
            else:
                assert len(sent) == 2
                assert all(row["state"] == "SENT"
                           for row in core.store.rows("SELECT state FROM outbox"))
            assert len(core.store.rows("SELECT * FROM voice_jobs")) == 1
            assert all(message["message"][0]["type"] == "record" for message in sent)
    run(scenario())
