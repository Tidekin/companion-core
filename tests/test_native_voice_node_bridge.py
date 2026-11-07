"""Real TLS Core, isolated voice nodes and checked Bridge delivery; external I/O is synthetic."""

import asyncio
import base64
import json
import pickle
import ssl
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from companion.voice_config import VoiceNode, VoiceReplyPolicy
from companion.voice_protocol import digest
from companion_onebot_bridge.network import private_http_client
from companion_voice_node.worker import local_manifest
from tests.conftest import run
from tests.sdk_reader_fixture import Provider, answer, call, worker_for
from tests.support.onebot_voice_media import URL, wav
from tests.support.onebot_voice_pipeline import capture, media_row, pump, tick
from tests.support.prepare_owner_input import native_spool, window
from tests.support.voice_jobs import TEXT, voice_config
from tests.support.voice_node_api import independent_node, tls_core
from tests.support.voice_node_worker import child_receipts
from tests.support.voice_node_worker import node as node  # noqa: F401 -- pytest fixture


@pytest.mark.parametrize("reply_mode", ["auto", "model", "mixed"])
def test_real_voice_nodes_feed_and_receive_native_bridge_custody(core, tmp_path, node, reply_mode):
    node_config, signing_key, counter = node
    node_config = node_config.model_copy(update={"node_id": "local",
        "request_timeout_seconds": 10.0, "heartbeat_seconds": 0.5,
        "execution_timeout_seconds": 20.0})
    core.settings.voice = voice_config(signing_private_key=signing_key.private_bytes_raw().hex(),
        reply=VoiceReplyPolicy(mode="model" if reply_mode == "mixed" else reply_mode),
        nodes={"local": VoiceNode(token=node_config.credential,
                                  manifest_sha256=digest(local_manifest(node_config)))})
    sent, checks = [], []

    def protocol(request):
        if request.url.path == "/get_record":
            data = {"file": URL, "url": URL, "file_size": str(len(wav())),
                    "file_name": "cache-123.amr"}
        elif request.url.path == "/send_private_msg":
            sent.append(json.loads(request.content))
            data = {"message_id": 987}
        else:
            data = {"/get_login_info": {"user_id": int(core.settings.bot_qq_id)},
                    "/get_status": {"online": True, "good": True},
                    "/get_version_info": {"app_name": "SnowLuma",
                                          "app_version": "2.0.3-node"}}[request.url.path]
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": data})

    async def inspect(request):
        if request.url.path.endswith("/owner-authority/send-check"):
            checks.append(json.loads(request.content))

    async def scenario():
        async with window(core, tmp_path, voice=True) as (_, config, plan), \
                tls_core(core, tmp_path) as (origin, certificate):
            config = replace(config, core_url=origin)
            spool, runtime = native_spool(core, tmp_path, config, plan)
            try:
                async with private_http_client(origin, config.bridge_token,
                    transport=httpx.AsyncHTTPTransport(trust_env=False,
                        verify=ssl.create_default_context(cafile=str(certificate)))) as client, \
                        private_http_client(config.onebot_http_url, config.onebot_token,
                            transport=httpx.MockTransport(protocol)) as native:
                    client.event_hooks["request"].append(inspect)
                    event = await capture(runtime, client, native)
                    original = core.store.one('SELECT body_json FROM events WHERE event_id=%s',
                                               (event["event_id"],))["body_json"]
                    await pump(runtime, client, native).execute(media_row(runtime))
                    worker_config = node_config.model_copy(update={"core_origin": origin})
                    await independent_node(worker_config, certificate, counter, tmp_path,
                                           core.clock.now.timestamp())
                    if reply_mode in {"model", "mixed"}:
                        job = core.store.one('SELECT job_id FROM jobs WHERE event_id=%s',
                                             (event["event_id"],))
                        args, output = tmp_path / "sdk.pickle", tmp_path / "sdk-requests.json"
                        parts = [{"kind": "voice", "text": TEXT}]
                        if reply_mode == "mixed":
                            parts.append({"kind": "text", "text": "这是语音之后的独立文字。"})
                        response = answer(calls=[call(
                            "voice-final", "reply_with_parts",
                            {"parts": parts})])
                        args.write_bytes(pickle.dumps((core.settings, core.clock.now,
                            job["job_id"], [response], "SUCCEEDED", str(output))))
                        process = await asyncio.create_subprocess_exec(sys.executable,
                            str(Path(__file__).with_name("reader_sdk_worker.py")), str(args),
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                        try:
                            logs, _ = await asyncio.wait_for(process.communicate(), 45)
                        except BaseException:
                            process.kill()
                            await process.wait()
                            raise
                        assert process.returncode == 0, logs.decode(errors="replace")
                        requests = json.loads(output.read_text(encoding="utf-8"))
                    else:
                        provider = Provider(answer(TEXT))
                        async with worker_for(core, provider) as worker:
                            assert await worker.run_once("native-voice-context")
                        requests = provider.requests
                    assert len(requests) == 1
                    assert "untrusted_transcription" in json.dumps(requests)
                    assert not await tick(runtime, client, native)
                    await independent_node(worker_config, certificate, counter, tmp_path,
                                           core.clock.now.timestamp(), scenario="large-result")
                    assert await tick(runtime, client, native)
                    assert len(sent) == len(checks) == 1
                    message = sent[0]["message"]
                    assert len(message) == 1 and message[0]["type"] == "record"
                    assert len(base64.b64decode(message[0]["data"]["file"][9:])) > 131072
                    assert checks[0]["authority"] == runtime.authority
                    assert checks[0]["voice"]["audio_bytes"] > 131072
                    if reply_mode == "mixed":
                        assert await tick(runtime, client, native)
                        assert sent[1]["message"] == [{"type": "text", "data": {
                            "text": "这是语音之后的独立文字。"}}]
                        assert "voice" not in checks[1]
                        assert not await tick(runtime, client, native)
                    assert core.store.rows("SELECT state,attempt_count FROM outbox") == [
                        {"state": "SENT", "attempt_count": 1}] * (
                            2 if reply_mode == "mixed" else 1)
                    assert core.store.rows(
                        'SELECT operation,state FROM voice_jobs ORDER BY record_sequence') == [
                        {"operation": "asr", "state": "TRANSCRIBED"},
                        {"operation": "tts", "state": "READY"}]
                    assert len(child_receipts(counter)) == 2
                    assert core.store.one('SELECT body_json FROM events WHERE event_id=%s',
                        (event["event_id"],))["body_json"] == original
                    assert not core.store.rows("SELECT * FROM memories")
                    runtime.verify()
            finally:
                spool.close()

    run(scenario())
