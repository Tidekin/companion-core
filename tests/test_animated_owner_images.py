"""Generated animation fixtures, authenticated intake, and synthetic provider transports."""

import asyncio
import base64
import hashlib
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from companion.contracts import CoreError
from companion.core import Core
from companion.image_input import text_and_estimate
from companion.reply_segments import SEMANTIC_RULES, SegmentedReplyConfig
from companion_onebot_bridge import app, images
from companion_onebot_bridge.event_normalizer import normalize
from companion_onebot_bridge.pending_media import MediaPump
from tests.conftest import run
from tests.native_continuous_support import frame, host, until
from tests.support.animated_owner_images import animation, policy
from tests.support.image_connect_recovery import ConnectThenGet
from tests.support.live_image_input import options as image_options
from tests.support.onebot_events import config, raw_event
from tests.support.onebot_images import image_segment


@pytest.mark.parametrize("subtype", [0, 1, 7, 11])
def test_normal_gif_and_qq_animated_emoji_keep_original_source_proof(subtype):
    segment = image_segment("https://gchat.qpic.cn/animated.gif")
    segment["data"].update(sub_type=subtype, file="synthetic.gif",
                           summary="synthetic-summary-not-visual-evidence")
    event = normalize(json.dumps(raw_event(message=[segment])), config(),
                      now=raw_event()["time"], rich_input_scope="references_v1",
                      image_scope="owner_image_v1")
    assert event["payload"]["shared_content"] == [{"kind": "image", "slot": 0}]
    assert event["payload"]["rich"]["segments"][0]["coverage"] == "UNREAD"
    assert "gchat.qpic.cn" not in json.dumps(event)
    assert "synthetic-summary-not-visual-evidence" not in json.dumps(event)


@pytest.mark.parametrize("format,mime", [("GIF", "image/gif"), ("WEBP", "image/webp"),
                                         ("PNG", "image/png"), ("PNG", "image/apng")])
def test_common_animation_formats_produce_bounded_ordered_static_sheet(format, mime):
    from companion.image_frames import prepare

    data = animation(format)
    prepared = prepare(data, mime, policy())
    assert prepared.mime == "image/png" and prepared.data != data
    assert "抽样" in prepared.note and "3" in prepared.note
    assert "1,2,3" in prepared.note
    with Image.open(io.BytesIO(prepared.data)) as sheet:
        assert sheet.n_frames == 1
        assert sheet.size == (36, 8)
        assert sheet.getpixel((2, 2))[:3] == (255, 0, 0)
        assert sheet.getpixel((26, 2))[:3] == (0, 0, 255)
    messages = [{"role": "user", "content": [
        {"type": "text", "text": prepared.note},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,"
                                              + base64.b64encode(prepared.data).decode()}},
    ]}]
    assert text_and_estimate(policy(), messages)[1] == 100


def test_animation_limits_and_content_type_confusion_fail_before_provider_io():
    from companion.image_frames import prepare

    with pytest.raises(CoreError, match="CONTENT_IMAGE_INVALID"):
        prepare(animation(), "image/png", policy())
    with pytest.raises(CoreError, match="CONTENT_IMAGE_INVALID"):
        prepare(animation(count=241), "image/gif", policy())
    with pytest.raises(CoreError, match="CONTENT_IMAGE_INVALID"):
        prepare(animation(size=(200, 200)), "image/gif", policy())


def test_apng_poster_is_not_an_animation_frame_and_sampling_includes_the_last_frame():
    from companion.image_frames import prepare

    output = io.BytesIO()
    frames = [Image.new("RGBA", (4, 4), color) for color in ("red", "green", "blue")]
    frames[0].save(output, format="PNG", save_all=True, append_images=frames[1:],
                   default_image=True, duration=[100, 200])
    prepared = prepare(output.getvalue(), "image/apng", policy())
    assert "共2帧" in prepared.note and "第1,2帧" in prepared.note
    assert "0,100" in prepared.note
    with Image.open(io.BytesIO(prepared.data)) as sheet:
        assert sheet.size == (8, 4)
        assert sheet.getpixel((1, 1)) == (0, 128, 0)
        assert sheet.getpixel((5, 1)) == (0, 0, 255)
    sampled = prepare(animation(count=10), "image/gif", policy())
    assert "共10帧" in sampled.note and "第1,3,5,6,8,10帧" in sampled.note


@pytest.mark.parametrize("format,mime,count,subtype", [
    ("GIF", "image/gif", 3, 11), ("WEBP", "image/webp", 3, 11),
    ("PNG", "image/apng", 3, 11), ("GIF", "image/gif", 3, 1),
    ("WEBP", "image/webp", 3, 1), ("PNG", "image/apng", 3, 1),
    ("PNG", "image/png", 1, 1),
    ("GIF", "image/gif", 3, 7), ("PNG", "image/png", 1, 7),
])
def test_real_api_and_independent_worker_reply_to_animation_then_continue_semantic_chat(
    core, tmp_path, monkeypatch, format, mime, count, subtype,
):
    from companion import __main__, api
    from companion import core as core_module

    original = animation(format, count=count, size=(3, 2))
    first_reply = "收到啦。\n它在动呢，几帧连着看很有意思。\n你想表达这个吗？"
    second_reply = "我在。\n接着聊。"
    calls, instances, downloads = [], [], []
    next_input = asyncio.Event()

    async def source(ws):
        segment = image_segment("https://gchat.qpic.cn/animation")
        segment["data"].update(sub_type=subtype, file="owner-animation",
                               summary="synthetic-summary-not-visual-evidence")
        await ws.send(frame(1, message=[segment]))
        closed = asyncio.create_task(ws.wait_closed())
        next_turn = asyncio.create_task(next_input.wait())
        try:
            await asyncio.wait({closed, next_turn}, return_when=asyncio.FIRST_COMPLETED)
            if closed.done():
                return
        finally:
            closed.cancel()
            next_turn.cancel()
            await asyncio.gather(closed, next_turn, return_exceptions=True)
        await ws.send(frame(2, text="接着刚才的话题聊"))
        await ws.wait_closed()

    def construct(*args, **kwargs):
        instance = Core(*args, clock=core.clock, **kwargs)
        instances.append(instance)
        return instance

    def respond(request):
        messages = json.loads(request.content)["messages"]
        calls.append(messages)
        text = first_reply if len(calls) == 1 else second_reply
        return httpx.Response(200, json={
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    def cdn(request):
        downloads.append(request)
        return httpx.Response(200, content=original, headers={"Content-Type": mime})

    async def resolve(*_):
        return ["1.1.1.1", "8.8.8.8"]

    fetch = images.fetch_image
    connection_attempts = []

    async def mocked_cdn(image, maximum, **kwargs):
        return await fetch(image, maximum, **{**kwargs, "resolver": resolve,
            "transport": ConnectThenGet(httpx.MockTransport(cdn), addresses=connection_attempts)})

    async def scenario():
        monkeypatch.setattr(api, "Core", construct)
        monkeypatch.setattr(core_module, "Core", construct)
        monkeypatch.setattr(images, "fetch_image", mocked_cdn)
        monkeypatch.setattr(__main__, "httpx", SimpleNamespace(AsyncClient=lambda **kwargs:
            httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(respond))))
        image = image_options()
        segmentation = SegmentedReplyConfig(mode="semantic", max_segments="unlimited",
                                            interval_ms=0)
        async with host(core, tmp_path, source, policy=segmentation,
            core_options={**image["core_options"], "rich_input_scope": "references_v1"},
            paid_options=image["paid_options"], purposes=("chat", "image", "history", "content"),
            bridge_options={"rich_input_scope": "references_v1"}, model_reply=respond,
            independent_api=True,
        ) as (active, _, sends, _):
            assert active.runtime.authority == active.runtime.config.owner_authority
            task = asyncio.create_task(__main__.worker(core.settings))
            active.start(worker=False)
            media = MediaPump(active.runtime, active.client, protocol=active.onebot)
            active.tasks.append(asyncio.create_task(app.transfer_media(media, active.stop)))
            try:
                await until(lambda: active.attempts() == {"ACK": 3},
                            tasks=[task, *active.tasks], deadline_seconds=30)
                assert len(calls) == 1
                next_input.set()
                await until(lambda: active.attempts() == {"ACK": 5},
                            tasks=[task, *active.tasks], deadline_seconds=30)
                assert active.captures() == 2
            except TimeoutError as error:
                raise AssertionError({"events": active.events,
                    "jobs": core.store.rows("SELECT kind,state,last_error_code FROM jobs"),
                    "content": core.store.rows("SELECT state FROM content_items"),
                    "attempts": active.attempts(), "calls": len(calls),
                    "downloads": len(downloads), "sends": len(sends)}) from error
            finally:
                await active.pause()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert len(instances) == 2 and instances[0].store is not instances[1].store
        assert len(calls) == 2 and len(downloads) == 1
        assert connection_attempts == ["1.1.1.1", "8.8.8.8"]
        assert [part["message"][0]["data"]["text"] for part in sends] == (
            first_reply.splitlines() + second_reply.splitlines()
        )
        image_parts = [part for message in calls[0] if isinstance(message["content"], list)
                       for part in message["content"] if part["type"] == "image_url"]
        assert len(image_parts) == 1
        sheet = base64.b64decode(image_parts[0]["image_url"]["url"].split(",", 1)[1])
        with Image.open(io.BytesIO(sheet)) as prepared:
            assert prepared.n_frames == 1 and prepared.size == (3 * count, 2)
        assert ("抽样" in json.dumps(calls[0], ensure_ascii=False)) == (count > 1)
        assert "synthetic-summary-not-visual-evidence" not in json.dumps(calls)
        assert {"role": "assistant", "content": first_reply} in calls[1]
        assert SEMANTIC_RULES in calls[1][0]["content"]
        content = core.store.one("SELECT * FROM content_items WHERE kind='qq_image'")
        assert content["state"] == "SUCCEEDED"
        assert content["media_sha256"] == hashlib.sha256(original).hexdigest()
        assert core.store.rows("SELECT scenario,state FROM model_usage ORDER BY rowid") == [
            {"scenario": "chat", "state": "SETTLED"},
            {"scenario": "chat", "state": "SETTLED"}]
        assert core.store.one("SELECT COUNT(*) n FROM captured_images")["n"] == 1
        proof = json.loads(core.store.one("SELECT proof_json FROM captured_images")["proof_json"])
        assert proof["authority"] == active.runtime.authority
        assert proof["media_sha256"] == hashlib.sha256(original).hexdigest()
        assert core.store.one("SELECT COUNT(*) n FROM model_admissions")["n"] == 2

    run(scenario())
