"""Real catalog discovery/annotation/offer to mixed audio, original meme and text."""

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx

from companion.autonomous_selector import native_selector
from companion.core import Core
from companion.image_input import ImageInputPolicy
from companion.model import ModelClient
from companion.reply_segments import SegmentedReplyConfig
from companion_onebot_bridge import app
from companion_onebot_bridge.network import private_http_client
from companion_onebot_bridge.owner_memes import MemePump
from tests.conftest import run
from tests.sdk_reader_fixture import answer, call
from tests.support.meme_core import CAPTION
from tests.support.meme_runtime import RULE, public_transport
from tests.support.native_meme_entry import native_entry
from tests.support.next_memes_catalog import png
from tests.support.next_memes_discovery import resolver
from tests.support.onebot import provider as onebot_provider
from tests.support.onebot_events import raw_event
from tests.support.private_companion import companion_options
from tests.support.voice_flow import finish_node
from tests.support.voice_jobs import voice_config


def test_current_catalog_meme_is_sent_between_voice_and_text(core, tmp_path, monkeypatch):
    def unexpected_failure(self, *args):
        raise

    monkeypatch.setattr(Core, "fail_job", unexpected_failure)
    now = datetime.now(UTC).replace(microsecond=0)
    core.clock.now = now
    number = 1
    asset = png()
    sha = hashlib.sha256(asset).hexdigest()
    public_calls, model_calls, sends = [], [], []

    async def source(ws):
        await ws.send(
            json.dumps(
                raw_event(number, text="太棒了，成功了！", time=int(core.clock.now.timestamp()))
            )
        )
        await ws.wait_closed()

    def model(request):
        body = json.loads(request.content)
        model_calls.append(body)
        if isinstance(body["messages"][0]["content"], list):
            caption = {
                **CAPTION,
                "description": "绿色庆祝图片，适合分享成功的开心心情",
                "visible_text": "",
                "tone": "celebratory",
                "suitable": ["celebrate", "playful"],
                "use_when": ["庆祝成功"],
                "tags": ["庆祝", "成功", "开心", "表情包"],
            }
            value = answer(json.dumps(caption, ensure_ascii=False))
        elif number == 1:
            value = answer("一起开心一下。")
        else:
            assert sha in json.dumps(body, ensure_ascii=False)
            value = answer(
                calls=[
                    call(
                        "mixed-final",
                        "reply_with_parts",
                        {
                            "parts": [
                                {"kind": "voice", "text": "太好了，替你开心！"},
                                {"kind": "meme", "candidate_id": sha},
                                {"kind": "text", "text": "这是图片后面的独立文字。"},
                            ]
                        },
                    )
                ]
            )
        return httpx.Response(200, json=value)

    def protocol(request):
        if request.url.path == "/send_private_msg":
            body = json.loads(request.content)
            if body["message"][0]["type"] == "record":
                assert len(body["message"]) == 1
                sends.append(body)
                return httpx.Response(
                    200, json={"status": "ok", "retcode": 0, "data": {"message_id": 123456}}
                )
        return onebot_provider(request, sends)

    options = companion_options(core)
    options["core_options"].update(
        meme_semantics_enabled=True,
        meme_discovery_enabled=True,
        meme_discovery_daily_download_bytes=32 * 1024 * 1024,
        meme_discovery_daily_storage_bytes=8 * 1024 * 1024,
        meme_caption_daily_micro_usd=50000,
        expression_selection_probability=1.0,
        voice=voice_config(reply={"mode": "model"}),
        segmented_reply=SegmentedReplyConfig(max_segments="unlimited", interval_ms=0),
    )
    options["paid_options"].update(
        max_output_tokens=4096,
        meme_input=ImageInputPolicy(
            approval_sha256="e" * 64,
            estimated_tokens_per_image=100,
            max_bytes_per_image=4000000,
            max_pixels_per_image=16000000,
            max_images_per_request=5,
        ),
    )

    async def scenario():
        nonlocal number
        async with native_entry(
            core,
            tmp_path,
            source,
            options=options,
            rules=(RULE,),
            public=public_transport(public_calls, semantic=False),
            resolver=resolver,
        ) as values:
            window, client, _, _, _ = values
            pump = MemePump(window.runtime, client)

            async def admit():
                await window.capture_once(client, native)
                stop, wake = asyncio.Event(), asyncio.Event()

                def accepted(payload):
                    stop.set()
                    wake.set()

                await app.upload(
                    client,
                    window.spool,
                    wake,
                    stop,
                    SimpleNamespace(accepted=accepted),
                    runtime=window.runtime,
                    memes=pump,
                )
                task = pump.query_task
                assert task is not None
                pump.tick(idle=False)
                await task
                pump.tick(idle=False)
                return pump.latest()[0]

            try:
                async with (
                    private_http_client(
                        window.config.onebot_http_url,
                        window.config.onebot_token,
                        transport=httpx.MockTransport(protocol),
                    ) as native,
                    httpx.AsyncClient(transport=httpx.MockTransport(model)) as provider,
                ):
                    with Core(
                        core.settings, ModelClient(core.settings, provider), core.clock
                    ) as worker:
                        await admit()
                        assert await worker.run_once("mixed-meme-initial")
                        assert (await window.send(client, native))["worked"]
                        pump.next_run = 0
                        pump.tick(idle=True)
                        await pump.task
                        assert (await window.runtime.memes.run_once(client))["status"] == (
                            "CAPTION_PENDING"
                        )
                        caption = next(
                            lane
                            for lane in native_selector(worker, "mixed-meme").lanes
                            if lane.name == "meme_caption"
                        )
                        assert (await caption.run()).progressed
                        assert (await window.runtime.memes.run_once(client))["status"] == (
                            "CAPTION_READY")
                        assert (await window.runtime.memes.run_once(client))["status"] == (
                            "SEMANTIC_READY")
                        stored = window.db.execute("SELECT data FROM meme_assets WHERE sha=?",
                                                   (sha,)).fetchone()
                        assert stored[0] == asset
                        number = 2
                        current = await admit()
                        assert await worker.run_once("mixed-meme-final")
                        before = core.store.rows("SELECT content_json,state FROM outbox "
                                                 "WHERE origin_event_id=? ORDER BY segment_index",
                                                 (current,))
                        assert [json.loads(row["content_json"]).get("reply_part", {}).get("kind")
                                for row in before] == ["voice", "meme", "text"], (
                            before, core.store.rows("SELECT kind,state,last_error_code FROM jobs"))
                        assert not (await window.send(client, native))["worked"]
                        finish_node(worker.voice.jobs)
                        for _ in range(3):
                            assert (await window.send(client, native))["worked"]
                        assert [value["message"][0]["type"] for value in sends] == [
                            "text",
                            "record",
                            "image",
                            "text",
                        ]
                        assert all(len(value["message"]) == 1 for value in sends)
                        encoded = sends[2]["message"][0]["data"]["file"].removeprefix("base64://")
                        assert base64.b64decode(encoded) == asset
                        rows = core.store.rows(
                            "SELECT state,attempt_count FROM outbox "
                            "WHERE origin_event_id=? ORDER BY segment_index",
                            (current,),
                        )
                        assert rows == [{"state": "SENT", "attempt_count": 1}] * 3
                        assert len(public_calls) == 5 and len(model_calls) == 3
                        assert not (await window.send(client, native))["worked"]
            finally:
                await pump.close()

    run(scenario())
