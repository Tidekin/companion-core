import json
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx

from companion.voice_config import VoiceReplyPolicy
from companion_onebot_bridge import pending_media
from companion_onebot_bridge.capture import prepare_owner_input
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.onebot_client import OneBotClient
from companion_onebot_bridge.outbound import deliver_tick
from companion_onebot_bridge.owner_send import OwnerOneBot, OwnerSendGuard
from tests.support.onebot_events import raw_event
from tests.support.onebot_voice_media import URL, public, segment, wav
from tests.support.prepare_owner_input import native_spool
from tests.support.prepare_owner_input import window as owner_window
from tests.support.voice_jobs import voice_config


async def capture(window, client, protocol, *, record=True, number=1, flush=True):
    body = {"message": [segment()]} if record else {"text": "请继续讲讲"}
    frame = json.dumps(raw_event(number, time=int(window.clock().timestamp()), **body))
    prepared = await prepare_owner_input(window, client, OneBotClient(window.config, protocol),
                                         frame, window.clock(), ("127.0.0.1", 9000))
    event = prepared["event"]
    window.persist_capture(event, frame, received_at=window.clock(), peer=("127.0.0.1", 9000),
                            media=prepared["media"])
    if flush:
        assert await window.flush_one(client)
    return event


@asynccontextmanager
async def fixture(core, tmp_path, *, native=None, voice=True, asr=True, ws_url=None):
    def reply(request):
        if native:
            response = native(request)
            if response is not None:
                return response
        if request.url.path == "/get_record":
            assert json.loads(request.content) == {"file": "cache-123.amr"}
            return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {
                "file": URL, "url": URL, "file_size": str(len(wav())),
                "file_name": "cache-123.amr",
            }})
        values = {"/get_login_info": {"user_id": int(core.settings.bot_qq_id)},
                  "/get_status": {"online": True, "good": True},
                  "/get_version_info": {"app_name": "SnowLuma", "app_version": "2.0.3-node"}}
        assert request.url.path in values, request.url.path
        return httpx.Response(200, json={"status": "ok", "retcode": 0,
                                       "data": values[request.url.path]})

    core.settings.voice = voice_config(reply=VoiceReplyPolicy(mode="auto"), asr={"enabled": asr})
    async with owner_window(core, tmp_path, voice=voice) as (_, config, plan):
        if ws_url:
            config = replace(config, onebot_ws_url=ws_url)
        spool, runtime = native_spool(core, tmp_path, config, plan)
        try:
            async with private_http_client(config.onebot_http_url, config.onebot_token,
                                           transport=httpx.MockTransport(reply)) as protocol, \
                    private_http_client(config.core_url, config.bridge_token) as client:
                yield runtime, client, protocol, [], plan
        finally:
            spool.close()


def media_row(runtime):
    return pending_media._execute(runtime, "SELECT * FROM automatic_media").fetchone()


def pump(window, client, protocol, *, download=None):
    return pending_media.MediaPump(window, client, protocol=OneBotClient(window.config, protocol),
        voice_resolver=public,
        voice_transport=httpx.MockTransport(download or (lambda request: httpx.Response(
            200, content=wav(), headers={"Content-Type": "audio/wav"}))))


async def tick(window, client, protocol):
    operation = OwnerSendGuard(window, client)
    return await deliver_tick(client, OwnerOneBot(operation, protocol),
                              window.spool, guard=operation)
